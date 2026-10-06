"""Show a finished card inside the terminal.

kitty graphics (kitty, Ghostty, WezTerm), iTerm2 inline images (iTerm2, WezTerm) and sixel
(foot, mlterm, xterm -ti vt340, recent Windows Terminal) when we can tell they are available;
otherwise a truecolor half-block preview. Override with --preview or STASHCARD_PREVIEW.
"""

from __future__ import annotations

import base64
import io
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

PROTOCOLS = ("auto", "kitty", "iterm", "sixel", "blocks", "none")


def detect(os_name: str | None = None) -> str:
    forced = os.environ.get("STASHCARD_PREVIEW", "").lower()
    if forced in PROTOCOLS and forced != "auto":
        return forced
    term = os.environ.get("TERM", "").lower()
    prog = os.environ.get("TERM_PROGRAM", "").lower()
    if os.environ.get("KITTY_WINDOW_ID") or term == "xterm-kitty" or prog == "ghostty" or "ghostty" in term:
        return "kitty"
    if prog in ("iterm.app", "wezterm") or os.environ.get("LC_TERMINAL", "").lower() == "iterm2":
        return "iterm"
    if "sixel" in term or term.startswith(("foot", "mlterm", "contour")):
        return "sixel"
    if (os_name or os.name) == "nt":
        # Windows Terminal speaks VT and 24-bit colour, so the half-block preview works there.
        # TODO(verify): Windows Terminal >= 1.22 also renders sixel; opt in with STASHCARD_PREVIEW=sixel.
        # The classic console (conhost) may not have VT enabled: skip the preview, print the path.
        return "blocks" if os.environ.get("WT_SESSION") or prog else "none"
    return "blocks"


def _fit(img: Image.Image, cols: int, max_rows: int, cell_ratio: float = 2.0) -> Image.Image:
    w = max(8, cols)
    h = int(img.height / img.width * w / cell_ratio * 2)
    if h > max_rows * 2:
        h = max_rows * 2
        w = int(img.width / img.height * h * cell_ratio / 2)
    return img.convert("RGB").resize((max(1, w), max(2, h - h % 2)), Image.Resampling.LANCZOS)


def blocks(img: Image.Image, cols: int = 80, max_rows: int = 48) -> str:
    """Two pixels per character cell with the upper half block and 24-bit colour."""
    im = _fit(img, cols, max_rows)
    px = im.load()
    out = []
    for y in range(0, im.height, 2):
        line = []
        for x in range(im.width):
            r1, g1, b1 = px[x, y]
            r2, g2, b2 = px[x, y + 1] if y + 1 < im.height else (r1, g1, b1)
            line.append(f"\x1b[38;2;{r1};{g1};{b1}m\x1b[48;2;{r2};{g2};{b2}m▀")
        out.append("".join(line) + "\x1b[0m")
    return "\n".join(out) + "\n"


def _png_b64(img: Image.Image, max_w: int = 1200) -> str:
    im = img.convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def kitty(img: Image.Image, cols: int) -> str:
    data = _png_b64(img)
    chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)]
    out = []
    for i, chunk in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        if i == 0:
            out.append(f"\x1b_Ga=T,f=100,c={cols},m={more};{chunk}\x1b\\")
        else:
            out.append(f"\x1b_Gm={more};{chunk}\x1b\\")
    return "".join(out) + "\n"


def iterm(img: Image.Image, cols: int) -> str:
    data = _png_b64(img)
    return f"\x1b]1337;File=inline=1;width={cols};preserveAspectRatio=1:{data}\x07\n"


def sixel(img: Image.Image, width_px: int = 800) -> str:
    """Minimal sixel encoder: 64-colour palette, run-length encoded bands."""
    im = img.convert("RGB")
    if im.width > width_px:
        im = im.resize((width_px, int(im.height * width_px / im.width)), Image.Resampling.LANCZOS)
    q = im.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[: 64 * 3]
    w, h = q.size
    px = q.load()
    out = ["\x1bPq", f'"1;1;{w};{h}']
    for i in range(len(pal) // 3):
        r, g, b = pal[i * 3 : i * 3 + 3]
        out.append(f"#{i};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")
    for band in range(0, h, 6):
        colours = {px[x, y] for y in range(band, min(band + 6, h)) for x in range(w)}
        for ci in sorted(colours):
            row = []
            for x in range(w):
                bits = 0
                for dy in range(6):
                    y = band + dy
                    if y < h and px[x, y] == ci:
                        bits |= 1 << dy
                row.append(chr(63 + bits))
            # run-length encode
            enc, prev, run = [], row[0], 0
            for ch in row + [None]:
                if ch == prev:
                    run += 1
                    continue
                enc.append(f"!{run}{prev}" if run > 3 else prev * run)
                prev, run = ch, 1
            out.append(f"#{ci}" + "".join(enc) + "$")
        out.append("-")
    out.append("\x1b\\")
    return "".join(out) + "\n"


def show(img_or_path, protocol: str = "auto", stream=None) -> str:
    """Render a preview to ``stream`` (stdout). Returns the protocol used ('none' if skipped)."""
    stream = stream or sys.stdout
    if protocol == "none":
        return "none"
    if protocol == "auto":
        if not getattr(stream, "isatty", lambda: False)():
            return "none"
        protocol = detect()
        if protocol == "none":
            return "none"
    img = Image.open(img_or_path) if isinstance(img_or_path, (str, Path)) else img_or_path
    cols = shutil.get_terminal_size((100, 40)).columns
    width = max(20, min(cols - 2, 110))
    if protocol == "kitty":
        payload = kitty(img, width)
    elif protocol == "iterm":
        payload = iterm(img, width)
    elif protocol == "sixel":
        payload = sixel(img)
    else:
        rows = shutil.get_terminal_size((100, 40)).lines
        payload = blocks(img, min(width, 96), max(16, min(rows - 6, 60)))
        protocol = "blocks"
    stream.write(payload)
    stream.flush()
    return protocol


def open_file(path: str | Path) -> bool:
    """Open a file with the system viewer. Quietly does nothing when there is none."""
    p = str(path)
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Windows":
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            opener = shutil.which("xdg-open") or shutil.which("gio")
            if not opener:
                return False
            args = [opener, p] if opener.endswith("xdg-open") else [opener, "open", p]
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
