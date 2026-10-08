import io

import pytest
from conftest import make_item
from PIL import Image

from stashcard.demo import DemoBackend
from stashcard.diff import compute_diff
from stashcard.games import GAMES, parse_games
from stashcard.pricing import ScanOptions
from stashcard.redact import Redaction
from stashcard.render import THEMES, render_report, save_png
from stashcard.render.canvas import Canvas, has_glyph
from stashcard.render.cards import render_banner, render_diff
from stashcard.render.icons import monogram, placeholder
from stashcard.render.theme import contrast, get_theme
from stashcard.scan import run_scan
from stashcard.term import preview


@pytest.fixture(scope="module")
def report():
    return run_scan(None, ScanOptions(games=parse_games(["cs2", "dota2", "tf2", "steam"])), DemoBackend("demo"))


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("mode", ["summary", "game", "grid", "receipt", "markets"])
def test_every_layout_renders(report, mode, theme):
    imgs = render_report(report, mode, theme=theme, game=GAMES["cs2"] if mode == "game" else None)
    assert imgs and all(isinstance(i, Image.Image) for i in imgs)
    img = imgs[0]
    assert img.width in (1200, 720) and 600 < img.height < 4000
    assert img.mode == "RGB"


def test_redacted_summary_and_paginated_grid(report):
    assert render_report(report, "summary", redaction=Redaction("full"))[0].width == 1200
    pages = render_report(report, "grid", limit=12, paginate=True)
    assert len(pages) == -(-len(report.items) // 12)


def test_diff_cards(report):
    then = run_scan(None, ScanOptions(games=report.opts.games), DemoBackend("demo", "then"))
    other = run_scan(None, ScanOptions(games=report.opts.games), DemoBackend("demo2"))
    for d in (compute_diff(then, report), compute_diff(report, other)):
        img = render_diff(d, get_theme("midnight"), None, Redaction("light"), Redaction("light"))
        assert img.width == 1200 and img.height > 700


def test_banner(report):
    assert render_banner(get_theme("midnight"), report=report).size == (1280, 360)


def test_png_has_no_text_chunks(tmp_path, report):
    img = render_report(report, "receipt")[0]
    img.info["Comment"] = "secret"
    path = save_png(img, tmp_path / "r.png")
    data = path.read_bytes()
    assert b"tEXt" not in data and b"iTXt" not in data and b"zTXt" not in data and b"eXIf" not in data
    assert Image.open(path).size == img.size


def test_long_names_are_truncated_to_fit():
    c = Canvas(400, 100, get_theme("paper"), ss=1)
    name = "StatTrak™ ★ Butterfly Knife | Gamma Doppler Emerald (Factory New) with a very long tail"
    fitted = c.fit(name, "sans_semi", 15, 220, middle=True)
    assert c.measure(fitted, "sans_semi", 15) <= 220 and fitted.endswith("tail") and "…" in fitted
    assert c.fit("short", "mono", 12, 300) == "short"


def test_star_falls_back_to_a_font_that_has_it():
    assert not has_glyph("mono", "★") and has_glyph("sans_med", "★")


def test_placeholders_and_monograms():
    assert monogram(make_item("AK-47 | Redline (Field-Tested)")) == "AK47"
    assert monogram(make_item("Mann Co. Supply Crate Key", "tf2")) == "MCS"
    img = placeholder(make_item("Gems", "steam", marketable=False), get_theme("midnight"), 128)
    assert img.size == (128, 128)


def test_theme_text_contrast():
    for t in THEMES.values():
        assert contrast(t.text, t.panel) >= 7
        assert contrast(t.dim, t.panel) >= 4.5, t.name


def test_terminal_previews(report):
    img = render_report(report, "summary")[0]
    out = io.StringIO()
    assert preview.show(img, "blocks", out) == "blocks" and "▀" in out.getvalue()
    assert preview.kitty(img, 80).startswith("\x1b_Ga=T,f=100")
    assert preview.iterm(img, 80).startswith("\x1b]1337;File=inline=1")
    small = img.resize((120, 90))
    six = preview.sixel(small, 120)
    assert six.startswith("\x1bPq") and six.rstrip().endswith("\x1b\\")
    assert preview.show(img, "auto", io.StringIO()) == "none"  # not a TTY


def test_preview_detection_per_platform(monkeypatch):
    for var in ("KITTY_WINDOW_ID", "TERM_PROGRAM", "LC_TERMINAL", "WT_SESSION", "STASHCARD_PREVIEW"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert preview.detect("posix") == "blocks"
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
    assert preview.detect("posix") == "iterm"
    monkeypatch.delenv("TERM_PROGRAM")
    assert preview.detect("nt") == "none"  # classic Windows console: path only
    monkeypatch.setenv("WT_SESSION", "x")
    assert preview.detect("nt") == "blocks"  # Windows Terminal
    monkeypatch.setenv("STASHCARD_PREVIEW", "sixel")
    assert preview.detect("nt") == "sixel"
