"""Remembered choices, your "me" profile, and optional secrets (kept locally, never logged).

settings.json   last choices from the interactive flow, plus your own SteamID64 ("me").
secrets.json    optional Steam Web API key, mode 0600.

Environment variables win over files: STASHCARD_STEAM_API_KEY.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from stashcard.paths import config_dir


@dataclass
class Settings:
    games: list[str] = field(default_factory=lambda: ["cs2", "dota2", "steam"])
    currency: str = "USD"
    source: str = "both"
    window: str = "7d"
    min_price: float = 0.0  # major units, as typed
    tradable_only: bool = False
    trimmed_avg: bool = False
    capture: float = 0.10
    skinport_fee: float = 0.08
    markets: list[str] = field(default_factory=lambda: ["steam", "skinport", "waxpeer", "csgotm", "lisskins"])
    lowest_from: str = "steam"
    theme: str = "midnight"
    redact: str = "auto"  # auto | none | light | full
    output_dir: str = "."
    open_after: bool = False
    mode: str = "summary"
    last_profile: str = ""
    me: str = ""  # your own SteamID64; your cards default to no redaction

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            raw = json.loads(cls.path().read_text("utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        try:
            return cls(**clean)
        except TypeError:
            return cls()

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), "utf-8")


def mask_secret(value: str | None) -> str:
    """Show just enough to recognise a secret: 'set (...a1b2)'. Never the whole value."""
    if not value:
        return "not set"
    tail = value[-4:] if len(value) >= 12 else ""
    return f"set (…{tail})" if tail else "set"


class Secrets:
    KEYS = ("api_key",)
    ENV = {"api_key": "STASHCARD_STEAM_API_KEY"}

    @staticmethod
    def path() -> Path:
        return config_dir() / "secrets.json"

    @classmethod
    def load(cls) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            raw = json.loads(cls.path().read_text("utf-8"))
            out.update({k: str(v) for k, v in raw.items() if k in cls.KEYS and v})
        except (OSError, ValueError):
            pass
        for key, env in cls.ENV.items():
            if os.environ.get(env):
                out[key] = os.environ[env]
        return out

    @classmethod
    def set(cls, key: str, value: str | None) -> None:
        if key not in cls.KEYS:
            raise KeyError(key)
        current: dict[str, str] = {}
        try:
            current = json.loads(cls.path().read_text("utf-8"))
        except (OSError, ValueError):
            pass
        if value:
            current[key] = value.strip()
        else:
            current.pop(key, None)
        path = cls.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def clear(cls) -> None:
        try:
            cls.path().unlink()
        except FileNotFoundError:
            pass
