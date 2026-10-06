"""Where Stashcard keeps settings, secrets, cache and scan history.

Set STASHCARD_HOME to keep everything under one folder (handy for tests and portable use).
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import PlatformDirs

_DIRS = PlatformDirs("stashcard", appauthor=False)


def _home() -> Path | None:
    value = os.environ.get("STASHCARD_HOME")
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    home = _home()
    return home / "config" if home else Path(_DIRS.user_config_dir)


def cache_dir() -> Path:
    home = _home()
    return home / "cache" if home else Path(_DIRS.user_cache_dir)


def data_dir() -> Path:
    home = _home()
    return home / "data" if home else Path(_DIRS.user_data_dir)
