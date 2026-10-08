"""A tiny on-disk cache: JSON documents and binary blobs (icons), each with a timestamp.

Stale entries are kept on purpose. When Steam rate-limits us, an old inventory is far
more useful than nothing, and the caller labels it with its age.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from stashcard.paths import cache_dir

# Default time-to-live per namespace, in seconds.
TTL = {
    "profile": 30 * 60,
    "inventory": 10 * 60,
    "steam_price": 3 * 3600,
    "steam_history": 6 * 3600,
    "skinport_items": 60 * 60,
    "skinport_history": 60 * 60,
    "venue_catalog": 60 * 60,
    "fx": 12 * 3600,
    "csfloat": 6 * 3600,
    "icons": None,  # forever
    "avatars": 24 * 3600,
}


@dataclass
class Hit:
    data: object
    age: float  # seconds

    @property
    def age_text(self) -> str:
        return human_age(self.age)


def human_age(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    if s < 36 * 3600:
        return f"{round(s / 3600)} h"
    return f"{round(s / 86400)} d"


def _digest(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Cache:
    def __init__(self, root: Path | None = None, enabled: bool = True):
        self.root = Path(root) if root else cache_dir()
        self.enabled = enabled

    # JSON -----------------------------------------------------------------------------
    def _json_path(self, ns: str, key: str) -> Path:
        return self.root / ns / f"{_digest(key)}.json"

    def get(self, ns: str, key: str, ttl: float | None | str = "default") -> Hit | None:
        """Return a cached document if it is younger than ``ttl`` seconds.

        ``ttl="default"`` uses the namespace default, ``ttl=None`` accepts any age.
        """
        if not self.enabled:
            return None
        path = self._json_path(ns, key)
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if doc.get("key") != key:
            return None
        age = time.time() - float(doc.get("saved_at", 0))
        limit = TTL.get(ns) if ttl == "default" else ttl
        if limit is not None and age > limit:
            return None
        return Hit(doc.get("data"), age)

    def put(self, ns: str, key: str, data: object) -> None:
        if not self.enabled:
            return
        doc = {"key": key, "saved_at": time.time(), "data": data}
        _atomic_write(self._json_path(ns, key), json.dumps(doc, ensure_ascii=False).encode("utf-8"))

    # Blobs ----------------------------------------------------------------------------
    def _blob_path(self, ns: str, key: str, ext: str) -> Path:
        return self.root / ns / f"{_digest(key)}.{ext}"

    def get_bytes(self, ns: str, key: str, ext: str = "png", ttl: float | None | str = "default") -> bytes | None:
        if not self.enabled:
            return None
        path = self._blob_path(ns, key, ext)
        try:
            st = path.stat()
        except OSError:
            return None
        limit = TTL.get(ns) if ttl == "default" else ttl
        if limit is not None and time.time() - st.st_mtime > limit:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def put_bytes(self, ns: str, key: str, data: bytes, ext: str = "png") -> None:
        if self.enabled:
            _atomic_write(self._blob_path(ns, key, ext), data)

    # Maintenance ----------------------------------------------------------------------
    def info(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        if not self.root.exists():
            return out
        for sub in sorted(p for p in self.root.iterdir() if p.is_dir()):
            files = [f for f in sub.iterdir() if f.is_file()]
            out[sub.name] = {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}
        return out

    def clear(self, ns: str | None = None) -> int:
        target = self.root / ns if ns else self.root
        if not target.exists():
            return 0
        count = sum(1 for p in target.rglob("*") if p.is_file())
        shutil.rmtree(target, ignore_errors=True)
        return count
