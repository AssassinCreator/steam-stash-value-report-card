"""Repository hygiene: everything ships in English."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile("[" + chr(0x0400) + "-" + chr(0x052F) + "]")  # Cyrillic + supplement
TEXT = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".cfg", ".svg"}


def test_no_cyrillic_anywhere():
    offenders = []
    for path in ROOT.rglob("*"):
        if any(part in (".venv", ".git", "__pycache__", ".pytest_cache") for part in path.parts):
            continue
        if path.is_file() and (path.suffix in TEXT or path.name in (".gitignore", ".editorconfig", "LICENSE")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if CYRILLIC.search(text):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
