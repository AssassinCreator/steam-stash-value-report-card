"""Card rendering entry points."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from stashcard.games import Game
from stashcard.redact import Redaction
from stashcard.render.theme import THEMES, Theme, get_theme
from stashcard.report import Report

__all__ = ["THEMES", "Theme", "get_theme", "render_report", "save_png"]


def render_report(
    report: Report,
    mode: str = "summary",
    *,
    theme: str | Theme = "midnight",
    backend=None,
    redaction: Redaction | None = None,
    game: Game | None = None,
    limit: int = 36,
    paginate: bool = False,
) -> list[Image.Image]:
    """Render one card (or several grid pages). Returns a list of images."""
    from stashcard.render import cards
    from stashcard.render.components import Ctx

    th = theme if isinstance(theme, Theme) else get_theme(theme)
    ctx = Ctx(report, th, backend, redaction or Redaction("none"))
    if mode == "summary":
        return [cards.render_summary(ctx)]
    if mode == "game":
        if game is None:
            totals = [g for g in report.games if g.game is not None]
            if not totals:
                raise ValueError("No game in this report.")
            game = max(totals, key=lambda g: g.lowest).game
        return [cards.render_game(ctx, game)]
    if mode == "grid":
        pages = cards.grid_pages(report, limit, paginate)
        return [cards.render_grid(ctx, items, i + 1, len(pages), limit) for i, items in enumerate(pages)]
    if mode == "receipt":
        return [cards.render_receipt(ctx)]
    if mode == "markets":
        return [cards.render_markets(ctx)]
    raise ValueError(f"Unknown card mode {mode!r}")


def save_png(img: Image.Image, path: str | Path) -> Path:
    """Save without any text chunks or metadata (nothing identifying rides along in the PNG)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = Image.new(img.mode, img.size)
    clean.paste(img)
    clean.info = {}
    clean.save(path, format="PNG", optimize=True)
    return path
