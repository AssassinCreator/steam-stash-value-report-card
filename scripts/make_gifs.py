"""Record the README GIFs.

    pip install -e ".[gifs]"          # pyte + pexpect, dev-only
    python scripts/make_gifs.py       # writes assets/demo.gif and assets/cards.gif

demo.gif   A real run of the interactive `stashcard` flow in a pseudo-terminal, driven by scripted
           keystrokes with a human typing cadence. The pty output is replayed through the pyte
           terminal emulator and drawn with the bundled JetBrains Mono and the real ANSI colours.
cards.gif  A slideshow of real demo cards (layouts and themes) with short crossfades.

Both are encoded with Pillow: adaptive palettes, identical frames merged, and only the changed
region of each frame stored. POSIX only (pexpect needs a pty).
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
FONTS = ROOT / "src" / "stashcard" / "fonts"

COLS, ROWS = 100, 38
FONT_PX = 15
LINE_H = 20
PAD = 18
TITLE_H = 34
MARGIN = 16

BG = (11, 15, 23)
FG = (216, 222, 233)
ANSI = {
    "black": (30, 36, 48),
    "red": (255, 122, 122),
    "green": (110, 231, 168),
    "brown": (245, 185, 74),
    "blue": (125, 170, 252),
    "magenta": (201, 162, 255),
    "cyan": (125, 211, 252),
    "white": (216, 222, 233),
    "brightblack": (86, 99, 122),
    "brightred": (255, 150, 150),
    "brightgreen": (124, 240, 182),
    "brightbrown": (255, 205, 110),
    "brightblue": (150, 190, 255),
    "brightmagenta": (220, 190, 255),
    "brightcyan": (160, 225, 255),
    "brightwhite": (245, 248, 252),
}
KEY = {"enter": "\r", "down": "\x1b[B", "up": "\x1b[A", "space": " "}
SPEED = 0.85  # scales the pauses between keystrokes


# --------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------
class Recorder:
    def __init__(self, cmd: list[str], env: dict, cwd: str):
        import pexpect
        import pyte

        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.child = pexpect.spawn(cmd[0], cmd[1:], env=env, cwd=cwd, dimensions=(ROWS, COLS))
        self.t0 = time.monotonic()
        self.frames: list[tuple[float, tuple, str]] = []
        self.marks: dict[str, float] = {}

    def snapshot(self) -> tuple:
        rows = []
        for y in range(ROWS):
            line = self.screen.buffer[y]
            rows.append(
                tuple((line[x].data, line[x].fg, line[x].bg, line[x].bold, line[x].reverse) for x in range(COLS))
            )
        cur = self.screen.cursor
        return tuple(rows), (cur.x, cur.y, cur.hidden)

    def pump(self, seconds: float) -> None:
        import pexpect

        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                # Small reads replay long output as the terminal really scrolls it, line by line.
                data = self.child.read_nonblocking(1536, timeout=min(0.03, left))
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
            self.stream.feed(data)
            snap = self.snapshot()
            if not self.frames or self.frames[-1][1] != snap:
                self.frames.append((time.monotonic() - self.t0, snap, "\n".join(self.screen.display)))

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def wait_for(self, needle: str, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        while needle not in self.text():
            if time.monotonic() > end:
                raise TimeoutError(f"never saw {needle!r}\n" + self.text())
            self.pump(0.05)

    def mark(self, name: str) -> None:
        self.marks[name] = time.monotonic() - self.t0

    def type(self, text: str) -> None:
        for ch in text:
            self.child.send(ch)
            self.pump(random.uniform(0.07, 0.16) * SPEED)

    def key(self, name: str, pause: float = 0.35, times: int = 1) -> None:
        for _ in range(times):
            self.child.send(KEY[name])
            self.pump((pause if times == 1 else random.uniform(0.16, 0.24)) * SPEED)
        if times > 1:
            self.pump(pause * SPEED)


def record_demo(stashcard: str, home: Path, workdir: Path) -> Recorder:
    random.seed(7)
    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "COLUMNS": str(COLS),
        "LINES": str(ROWS),
        "STASHCARD_HOME": str(home),
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT",
        "NO_COLOR": "",
        "PYTHONUNBUFFERED": "1",
        "STASHCARD_DEMO_PACE": "0.25",
    }  # network-like latency so the progress steps are watchable
    env.pop("NO_COLOR")
    r = Recorder([stashcard], env, str(workdir))
    r.wait_for("Steam profile")
    r.pump(0.7)
    r.type("demo")
    r.pump(0.4)
    r.key("enter", 0.5)
    r.wait_for("What do you want?")
    r.pump(0.35)
    r.key("up", 0.45, times=7)  # Demo -> Report card
    r.key("enter", 0.45)
    r.wait_for("Use saved settings?")
    r.pump(0.4)
    r.type("n")
    r.wait_for("Games")
    r.pump(0.4)
    r.key("down", 0.2, times=2)
    r.key("space", 0.5)  # add TF2
    r.key("enter", 0.5)
    r.wait_for("Currency")
    r.key("enter", 0.35)  # USD
    r.wait_for("Price source")
    r.key("enter", 0.35)
    r.wait_for("Marketplaces")
    r.pump(0.4)
    r.key("down", 0.2, times=5)
    r.key("space", 0.6)  # opt into CSFloat (experimental)
    r.key("enter", 0.5)
    r.wait_for("LOWEST comes from")
    r.key("down", 0.5)
    r.key("enter", 0.5)  # best
    r.wait_for("AVG window")
    r.key("enter", 0.3)
    r.wait_for("Minimum price")
    r.key("enter", 0.25)
    r.wait_for("Only tradable")
    r.key("enter", 0.25)
    r.wait_for("Advanced")
    r.key("enter", 0.3)
    r.wait_for("Card theme")
    r.pump(0.3)
    r.key("enter", 0.5)  # midnight
    r.wait_for("Privacy on the card")
    r.key("enter", 0.3)
    r.wait_for("Save cards in")
    r.key("enter", 0.3)
    r.wait_for("Open the image")
    r.key("enter", 0.2)
    r.mark("run")
    r.wait_for("honesty checks", 60)
    r.mark("report")
    r.wait_for("saved", 90)
    r.pump(1.0)
    r.mark("end")
    r.child.close()
    return r


# --------------------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------------------
def _font(name: str) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), FONT_PX)


REG, BOLD = _font("JetBrainsMono-Regular.ttf"), _font("JetBrainsMono-Bold.ttf")
FALLBACK = ImageFont.truetype(str(FONTS / "Inter-Medium.ttf"), FONT_PX)
CELL_W = REG.getlength("M")
TERM_W = int(round(COLS * CELL_W)) + 2 * PAD
TERM_H = ROWS * LINE_H + 2 * PAD // 2 + TITLE_H
WIN_W, WIN_H = TERM_W + 2 * MARGIN, TERM_H + 2 * MARGIN
_MISSING: dict[str, bool] = {}


def _color(value, default):
    if value in (None, "default"):
        return default
    if value in ANSI:
        return ANSI[value]
    if isinstance(value, str) and len(value) == 6:
        try:
            return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default
    return default


def _has_glyph(ch: str) -> bool:
    if ch not in _MISSING:
        notdef = bytes(REG.getmask("\U0010fffd"))
        _MISSING[ch] = bytes(REG.getmask(ch)) == notdef and not ch.isspace()
    return not _MISSING[ch]


def chrome(title: str) -> Image.Image:
    img = Image.new("RGB", (WIN_W, WIN_H), (6, 8, 12))
    shadow = Image.new("L", (WIN_W, WIN_H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((MARGIN, MARGIN + 4, WIN_W - MARGIN, WIN_H - MARGIN + 4), 12, fill=150)
    img.paste((0, 0, 0), mask=shadow.filter(ImageFilter.GaussianBlur(8)))
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = MARGIN, MARGIN, WIN_W - MARGIN, WIN_H - MARGIN
    d.rounded_rectangle((x0, y0, x1, y1), 12, fill=BG, outline=(34, 48, 71))
    d.rounded_rectangle((x0, y0, x1, y0 + TITLE_H), 12, fill=(19, 25, 37))
    d.rectangle((x0, y0 + TITLE_H - 12, x1, y0 + TITLE_H), fill=(19, 25, 37))
    d.line((x0, y0 + TITLE_H, x1, y0 + TITLE_H), fill=(34, 48, 71))
    for i, col in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
        cx = x0 + 20 + i * 20
        d.ellipse((cx - 6, y0 + TITLE_H / 2 - 6, cx + 6, y0 + TITLE_H / 2 + 6), fill=col)
    tf = ImageFont.truetype(str(FONTS / "JetBrainsMono-Medium.ttf"), 13)
    d.text(((x0 + x1) / 2, y0 + TITLE_H / 2), title, font=tf, fill=(147, 161, 181), anchor="mm")
    return img


_BRAILLE = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3)]  # dot -> (col, row)


def _braille(d: ImageDraw.ImageDraw, ch: str, left: float, top: float, color) -> None:
    """Braille spinner frames, drawn as dots (no bundled font has the Braille block)."""
    bits = ord(ch) - 0x2800
    for n, (col, row) in enumerate(_BRAILLE):
        if bits & (1 << n):
            x = left + 2.5 + col * 4
            y = top + 3.5 + row * 4
            d.ellipse((x - 1.3, y - 1.3, x + 1.3, y + 1.3), fill=color)


def draw_screen(base: Image.Image, snap: tuple, show_cursor: bool) -> Image.Image:
    rows, (cx, cy, hidden) = snap
    img = base.copy()
    d = ImageDraw.Draw(img)
    ox = MARGIN + PAD
    oy = MARGIN + TITLE_H + PAD // 2
    for y, row in enumerate(rows):
        top = oy + y * LINE_H
        x = 0
        while x < COLS:
            ch, fg, bg, bold, rev = row[x]
            fgc, bgc = _color(fg, FG), _color(bg, BG)
            if rev:
                fgc, bgc = bgc, fgc
            left = ox + x * CELL_W
            if bgc != BG:
                d.rectangle((left, top, left + CELL_W + 0.5, top + LINE_H), fill=bgc)
            if ch in ("▀", "▄", "█"):
                # Draw block elements as exact rectangles so the half-block preview has no seams.
                mid = top + LINE_H / 2
                if ch == "▀":
                    d.rectangle((left, top, left + CELL_W + 0.5, mid), fill=fgc)
                elif ch == "▄":
                    d.rectangle((left, mid, left + CELL_W + 0.5, top + LINE_H), fill=fgc)
                else:
                    d.rectangle((left, top, left + CELL_W + 0.5, top + LINE_H), fill=fgc)
            elif 0x2800 <= ord(ch) <= 0x28FF:
                _braille(d, ch, left, top, fgc)
            elif ch.strip():
                font = (BOLD if bold else REG) if _has_glyph(ch) else FALLBACK
                d.text((left, top + LINE_H - 5), ch, font=font, fill=fgc, anchor="ls")
            x += 1
    if show_cursor and not hidden and 0 <= cy < ROWS and 0 <= cx < COLS:
        left = ox + cx * CELL_W
        top = oy + cy * LINE_H
        d.rectangle((left, top + 2, left + CELL_W - 1, top + LINE_H - 2), fill=(124, 240, 182))
    return img


# --------------------------------------------------------------------------------------
# GIF encoding
# --------------------------------------------------------------------------------------
def to_palette(img: Image.Image, dither: bool, colors: int = 256) -> Image.Image:
    """Exact palette when the frame has few colours (terminal text); otherwise a median-cut palette
    refined with k-means, applied with Floyd-Steinberg dithering when asked (card gradients)."""
    found = img.getcolors(colors)
    if found is not None:
        flat = [c for _, rgb in found for c in rgb]
        pal = Image.new("P", (1, 1))
        pal.putpalette(flat + [0] * (768 - len(flat)))
        return img.quantize(palette=pal, dither=Image.Dither.NONE)
    base = img.quantize(colors, method=Image.Quantize.MEDIANCUT, kmeans=2)
    return img.quantize(palette=base, dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)


def save_gif(
    frames: list[Image.Image], durations: list[int], path: Path, colors: int = 256, dither: bool | list[bool] = False
) -> None:
    """Adaptive palette per frame, identical frames merged; Pillow stores only the changed region."""
    flags = dither if isinstance(dither, list) else [dither] * len(frames)
    merged: list[list] = []
    for f, dur, dz in zip(frames, durations, flags):
        if merged and f.tobytes() == merged[-1][0].tobytes():
            merged[-1][1] += dur
            continue
        merged.append([f, dur, dz])
    pal = [to_palette(f, dz, colors) for f, _, dz in merged]
    path.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(
        path,
        save_all=True,
        append_images=pal[1:],
        duration=[max(30, d) for _, d, _ in merged],
        loop=0,
        optimize=False,
        disposal=1,
    )


def make_demo_gif(stashcard: str, out: Path, home: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="stashcard-gif-"))
    rec = record_demo(stashcard, home, work)
    title = "stashcard  —  ~/cards"
    base = chrome(title)
    times = [f[0] for f in rec.frames] + [rec.marks.get("end", rec.frames[-1][0]) + 0.1]
    # Hold two complete states so the tables can be read: the per-game summary (the last frame that
    # still shows its header) and the full report (the last frame before the inline preview starts).
    holds = {}
    for need, avoid, ms in (("Unpriced", "\u2580", 2300), ("Since last scan", "\u2580", 2000)):
        idx = [i for i, f in enumerate(rec.frames) if need in f[2] and avoid not in f[2]]
        if idx:
            holds[idx[-1]] = ms
    frames, durs = [], []
    for i, (t, snap, _text) in enumerate(rec.frames):
        last = i == len(rec.frames) - 1
        dur = int((times[i + 1] - t) * 1000)
        if dur < 40 and not last and i not in holds:
            continue  # coalesce bursts (GIF players clamp tiny delays)
        frames.append(draw_screen(base, snap, show_cursor=True))
        if last:
            durs.append(3000)
        elif i in holds:
            durs.append(holds[i])
        else:
            durs.append(min(max(dur, 40), 900))
    save_gif(frames, durs, out)
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(frames)} frames, {sum(durs) / 1000:.1f}s)")


def make_cards_gif(out: Path, cards: list[tuple[Path, str]], size=(1000, 720)) -> None:
    W, H = size
    caption_h = 40
    slides = []
    cap_font = ImageFont.truetype(str(FONTS / "JetBrainsMono-Medium.ttf"), 14)
    for path, caption in cards:
        card = Image.open(path).convert("RGB")
        area_w, area_h = W - 60, H - caption_h - 30
        scale = min(area_w / card.width, 1.0)
        if card.width * scale < area_w and card.width > card.height:
            scale = area_w / card.width
        im = card.resize((int(card.width * scale), int(card.height * scale)), Image.Resampling.LANCZOS)
        if im.height > area_h:  # tall cards: show the top and fade out
            im = im.crop((0, 0, im.width, area_h))
            fade = Image.linear_gradient("L").resize((im.width, 90))
            edge = im.getpixel((4, im.height - 1))  # fade into the card's own background colour
            bg = Image.new("RGB", im.size, edge)
            mask = Image.new("L", im.size, 255)
            mask.paste(Image.eval(fade, lambda v: 255 - v), (0, im.height - 90))
            im = Image.composite(im, bg, mask)
        slide = Image.new("RGB", (W, H), (11, 15, 23))
        d = ImageDraw.Draw(slide)
        slide.paste(im, ((W - im.width) // 2, 15))
        d.line((30, H - caption_h, W - 30, H - caption_h), fill=(34, 48, 71))
        d.rectangle((30, H - caption_h + 14, 38, H - caption_h + 26), fill=(124, 240, 182))
        d.text((48, H - caption_h / 2), caption, font=cap_font, fill=(216, 222, 233), anchor="lm")
        d.text(
            (W - 30, H - caption_h / 2),
            "stashcard demo · fictional profile",
            font=cap_font,
            fill=(86, 99, 122),
            anchor="rm",
        )
        slides.append(slide)
    frames, durs, dz = [], [], []
    steps = 4
    for i, s in enumerate(slides):
        frames.append(s)
        durs.append(2300)
        dz.append(True)  # held slides are dithered: card gradients band without it
        nxt = slides[(i + 1) % len(slides)]
        for k in range(1, steps + 1):
            frames.append(Image.blend(s, nxt, k / (steps + 1)))
            durs.append(70)
            dz.append(False)  # crossfade frames flash by; skip dithering to keep the file small
    save_gif(frames, durs, out, dither=dz)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(frames)} frames, {sum(durs) / 1000:.1f}s)")


def render_cards(home: Path, outdir: Path, stashcard: str) -> list[tuple[Path, str]]:
    env = {**os.environ, "STASHCARD_HOME": str(home)}
    jobs = [
        ("summary", "midnight", "report card · midnight", []),
        ("markets", "midnight", "markets · midnight", []),
        ("grid", "paper", "inventory picture · paper", ["--limit", "18"]),
        ("receipt", "paper", "receipt · paper", []),
        ("diff", "phosphor", "then vs now · phosphor", []),
        ("summary", "phosphor", "report card · phosphor · full redaction", ["--redact", "full"]),
    ]
    out = []
    for n, (mode, theme, caption, extra) in enumerate(jobs):
        png = outdir / f"slide-{n}-{mode}-{theme}.png"
        subprocess.run(
            [
                stashcard,
                "demo",
                "--mode",
                mode,
                "--theme",
                theme,
                "-o",
                str(png),
                "-q",
                "--no-preview",
                "--no-sidecar",
                *extra,
            ],
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
        )
        out.append((png, caption))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("demo", "cards"))
    ap.add_argument("--home", help="STASHCARD_HOME to use (keeps the icon cache warm between runs)")
    args = ap.parse_args()
    stashcard = shutil.which("stashcard", path=str(Path(sys.executable).parent)) or shutil.which("stashcard")
    if not stashcard:
        print("stashcard is not installed in this environment", file=sys.stderr)
        return 1
    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="stashcard-home-"))
    # Warm the icon cache so the recording is not waiting on the network.
    subprocess.run(
        [stashcard, "demo", "--mode", "grid", "-o", str(home / "warm.png"), "-q", "--no-preview", "--no-sidecar"],
        env={**os.environ, "STASHCARD_HOME": str(home)},
        check=False,
        stdout=subprocess.DEVNULL,
    )
    if args.only in (None, "demo"):
        make_demo_gif(stashcard, ASSETS / "demo.gif", home)
    if args.only in (None, "cards"):
        tmp = Path(tempfile.mkdtemp(prefix="stashcard-slides-"))
        make_cards_gif(ASSETS / "cards.gif", render_cards(home, tmp, stashcard))
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
