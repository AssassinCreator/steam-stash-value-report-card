"""Regenerate the PNG and SVG assets in assets/ from the demo, with Stashcard's own renderer.
(The GIFs come from scripts/make_gifs.py.)

    python scripts/make_assets.py            # uses cached/downloaded item icons
    python scripts/make_assets.py --offline  # placeholders only

The terminal SVG is a real `stashcard demo` run recorded with Rich (Console(record=True)).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from stashcard.cache import Cache
from stashcard.cli import IconFetcher
from stashcard.demo import DemoBackend
from stashcard.diff import compute_diff, delta_from
from stashcard.games import GAMES, parse_games
from stashcard.net import Net
from stashcard.pricing import ScanOptions
from stashcard.redact import Redaction
from stashcard.render import render_report, save_png
from stashcard.render.cards import render_banner, render_diff
from stashcard.render.theme import get_theme
from stashcard.scan import Steps, run_scan
from stashcard.term.ui import GOOD, print_header, print_report

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
GAMES4 = ["cs2", "dota2", "tf2", "steam"]


class LogSteps(Steps):
    """Prints each finished step as a tick line (a static stand-in for the live spinner)."""

    def __init__(self, console):
        self.console = console

    def done(self, key, label=None, ok=True):
        line = Text("✓ " if ok else "✗ ", style=GOOD if ok else "red")
        line.append(label or key)
        self.console.print(line)


def scan(name="demo", epoch="now", icons=None, steps=None):
    opts = ScanOptions(games=parse_games(GAMES4))
    return run_scan(None, opts, DemoBackend(name, epoch, icons=icons), steps=steps)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    ASSETS.mkdir(exist_ok=True)
    icons = IconFetcher(Net(offline=args.offline, timeout=8, retries=0, max_wait=0), Cache())

    # 1. Terminal session, recorded.
    console = Console(
        record=True, width=112, force_terminal=True, color_system="truecolor", highlight=False, emoji=False
    )
    console.print(Text("$ stashcard demo", style="bold"))
    print_header(console)
    now = scan(icons=icons, steps=LogSteps(console))
    then = scan("demo", "then")
    now.delta = delta_from(then, now)
    print_report(console, now, Redaction("none"), top=8)
    console.print(Text("saved stashcard-demo-summary.png  (+ .json, .alt.txt)", style="#93A1B5"))
    console.save_svg(str(ASSETS / "terminal.svg"), title="stashcard demo")

    # 2. Banner.
    save_png(render_banner(get_theme("midnight"), report=now, backend=icons), ASSETS / "banner.png")

    # 3. Gallery.
    jobs = [
        ("card-summary-midnight.png", "summary", "midnight", "none", None),
        ("card-summary-paper.png", "summary", "paper", "none", None),
        ("card-summary-redacted.png", "summary", "phosphor", "full", None),
        ("card-game-cs2.png", "game", "midnight", "none", GAMES["cs2"]),
        ("card-game-dota2-paper.png", "game", "paper", "none", GAMES["dota2"]),
        ("card-grid.png", "grid", "midnight", "none", None),
        ("card-receipt.png", "receipt", "paper", "none", None),
        ("card-markets.png", "markets", "midnight", "none", None),
    ]
    for name, mode, theme, red, game in jobs:
        img = render_report(now, mode, theme=theme, backend=icons, redaction=Redaction(red), game=game, limit=24)[0]
        save_png(img, ASSETS / name)
        print("wrote", name, img.size)
    timeline = compute_diff(then, now)
    save_png(render_diff(timeline, get_theme("phosphor"), icons), ASSETS / "card-then-vs-now.png")
    versus = compute_diff(now, scan("demo2"))
    save_png(
        render_diff(versus, get_theme("midnight"), icons, Redaction("light"), Redaction("light")),
        ASSETS / "card-a-vs-b.png",
    )
    print("wrote diff cards, banner.png, terminal.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
