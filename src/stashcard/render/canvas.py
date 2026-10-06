"""Drawing toolkit. Layouts work in logical pixels; the canvas renders at ``ss``x and
downsamples with Lanczos at the end, which gives clean anti-aliasing for shapes and text.
"""

from __future__ import annotations

import random
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from stashcard.render.theme import Theme, hex_rgb, mix

FONT_FILES = {
    "sans": "Inter-Regular.ttf",
    "sans_med": "Inter-Medium.ttf",
    "sans_semi": "Inter-SemiBold.ttf",
    "sans_bold": "Inter-Bold.ttf",
    "display": "InterDisplay-ExtraBold.ttf",
    "display_bold": "InterDisplay-Bold.ttf",
    "mono": "JetBrainsMono-Regular.ttf",
    "mono_med": "JetBrainsMono-Medium.ttf",
    "mono_bold": "JetBrainsMono-Bold.ttf",
    "mono_xbold": "JetBrainsMono-ExtraBold.ttf",
}
# When a glyph is missing (JetBrains Mono has no star, Inter no box drawing) try the twin.
FALLBACK = {
    "mono": "sans_med",
    "mono_med": "sans_med",
    "mono_bold": "sans_bold",
    "mono_xbold": "sans_bold",
    "sans": "mono",
    "sans_med": "mono_med",
    "sans_semi": "mono_bold",
    "sans_bold": "mono_bold",
    "display": "sans_bold",
    "display_bold": "sans_bold",
}
ELLIPSIS = "…"


def font_path(role: str) -> Path:
    return Path(str(resources.files("stashcard.fonts").joinpath(FONT_FILES[role])))


@lru_cache(maxsize=256)
def load_font(role: str, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path(role)), max(1, px))


@lru_cache(maxsize=64)
def _notdef(role: str) -> bytes:
    f = load_font(role, 32)
    return bytes(f.getmask("\U0010fffd"))


@lru_cache(maxsize=8192)
def has_glyph(role: str, ch: str) -> bool:
    if ch.isspace():
        return True
    f = load_font(role, 32)
    return bytes(f.getmask(ch)) != _notdef(role)


def runs(text: str, role: str) -> list[tuple[str, str]]:
    """Split text into (role, chunk) runs so missing glyphs come from the fallback font."""
    alt = FALLBACK.get(role)
    out: list[tuple[str, str]] = []
    for ch in text:
        use = role if (alt is None or has_glyph(role, ch) or not has_glyph(alt, ch)) else alt
        if out and out[-1][0] == use:
            out[-1] = (use, out[-1][1] + ch)
        else:
            out.append((use, ch))
    return out


class Canvas:
    def __init__(self, width: int, height: int, theme: Theme, ss: int = 2, background: bool = True):
        self.w, self.h, self.ss, self.theme = width, height, ss, theme
        self.img = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.img)
        if background:
            self.background()

    # Units ------------------------------------------------------------------------------
    def s(self, v: float) -> int:
        return int(round(v * self.ss))

    def box(self, b) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = b
        return self.s(x0), self.s(y0), self.s(x1) - 1, self.s(y1) - 1

    @staticmethod
    def col(c, alpha: int | None = None):
        if isinstance(c, str):
            return hex_rgb(c, 255 if alpha is None else alpha)
        if alpha is not None:
            return (*c[:3], alpha)
        return c if len(c) == 4 else (*c, 255)

    # Background -------------------------------------------------------------------------
    def background(self) -> None:
        t = self.theme
        self.vgradient((0, 0, self.w, self.h), t.bg_top, t.bg_bottom)
        if t.grain:
            self.grain(t.grain)
        if t.scanlines:
            self.scanlines()

    def vgradient(self, b, top, bottom, radius: float = 0, alpha: int = 255) -> None:
        x0, y0, x1, y1 = self.box(b)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if w <= 0 or h <= 0:
            return
        grad = Image.linear_gradient("L").resize((1, h))
        c0, c1 = self.col(top, alpha), self.col(bottom, alpha)
        layer = Image.composite(Image.new("RGBA", (1, h), c1), Image.new("RGBA", (1, h), c0), grad).resize((w, h))
        mask = None
        if radius:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), self.s(radius), fill=255)
        self._paste(layer, (x0, y0), mask)

    def hgradient(self, b, left, right, radius: float = 0) -> None:
        x0, y0, x1, y1 = self.box(b)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if w <= 0 or h <= 0:
            return
        grad = Image.linear_gradient("L").rotate(90, expand=True).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        grad = grad.resize((w, 1))
        c0, c1 = self.col(left), self.col(right)
        layer = Image.composite(Image.new("RGBA", (w, 1), c1), Image.new("RGBA", (w, 1), c0), grad).resize((w, h))
        mask = None
        if radius:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), self.s(radius), fill=255)
        self._paste(layer, (x0, y0), mask)

    def _paste(self, layer: Image.Image, xy: tuple[int, int], mask: Image.Image | None = None) -> None:
        if mask is not None:
            alpha = ImageChops.multiply(layer.getchannel("A"), mask)
            layer = layer.copy()
            layer.putalpha(alpha)
        self.img.alpha_composite(layer, xy)

    def grain(self, strength: float) -> None:
        w, h = self.img.size
        small = (max(1, w // 3), max(1, h // 3))
        rng = random.Random(7)
        noise = Image.frombytes("L", small, bytes(rng.randrange(256) for _ in range(small[0] * small[1])))
        noise = noise.resize((w, h), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(0.6))
        dark = Image.new("RGBA", (w, h), (60, 45, 20, 0))
        dark.putalpha(noise.point(lambda v: int(max(0, v - 150) * 0.35 * strength)))
        self.img.alpha_composite(dark)

    def scanlines(self) -> None:
        w, h = self.img.size
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        step = self.s(3)
        for y in range(0, h, step):
            d.rectangle((0, y, w, y + max(1, self.ss // 2)), fill=(0, 0, 0, 70))
        self.img.alpha_composite(layer)
        # soft vignette
        vig = Image.new("L", (w, h), 0)
        ImageDraw.Draw(vig).ellipse((-w * 0.25, -h * 0.35, w * 1.25, h * 1.35), fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(self.s(80)))
        shade = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shade.putalpha(vig.point(lambda v: int((255 - v) * 0.45)))
        self.img.alpha_composite(shade)

    # Shapes -----------------------------------------------------------------------------
    def rrect(self, b, radius: float, fill=None, outline=None, width: float = 1) -> None:
        if fill is not None and isinstance(fill, (str, tuple)) and len(self.col(fill)) == 4 and self.col(fill)[3] < 255:
            layer = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle(self.box(b), self.s(radius), fill=self.col(fill))
            self.img.alpha_composite(layer)
            fill = None
        if fill is None and outline is None:
            return
        self.draw.rounded_rectangle(
            self.box(b),
            self.s(radius),
            fill=self.col(fill) if fill is not None else None,
            outline=self.col(outline) if outline is not None else None,
            width=max(1, self.s(width)) if outline is not None else 0,
        )

    def panel(self, b, radius: float = 18, *, shadow: bool = True, border: bool = True, fill=None) -> None:
        t = self.theme
        if shadow:
            self.shadow(b, radius, blur=18 if t.dark else 14, alpha=110 if t.dark else 38, dy=8)
        if fill is not None:
            self.rrect(b, radius, fill=fill)
        else:
            self.vgradient(b, t.panel_hi, t.panel, radius=radius)
        if border:
            self.rrect(b, radius, outline=t.border, width=1)

    def shadow(self, b, radius: float, blur: float = 16, alpha: int = 90, dy: float = 6, color=(0, 0, 0)) -> None:
        x0, y0, x1, y1 = self.box(b)
        pad = self.s(blur * 2)
        w, h = x1 - x0 + 1 + pad * 2, y1 - y0 + 1 + pad * 2
        layer = Image.new("RGBA", (w, h), (*color[:3], 0))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((pad, pad, w - pad, h - pad), self.s(radius), fill=alpha)
        mask = mask.filter(ImageFilter.GaussianBlur(self.s(blur)))
        layer.putalpha(mask)
        self.img.alpha_composite(layer, (x0 - pad, y0 - pad + self.s(dy)))

    def glow(self, center, radius: float, color, alpha: int = 90) -> None:
        cx, cy = self.s(center[0]), self.s(center[1])
        r = self.s(radius)
        size = r * 2
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((r * 0.35, r * 0.35, size - r * 0.35, size - r * 0.35), fill=alpha)
        mask = mask.filter(ImageFilter.GaussianBlur(r * 0.32))
        layer = Image.new("RGBA", (size, size), self.col(color))
        layer.putalpha(mask)
        self.img.alpha_composite(layer, (cx - r, cy - r))

    def line(self, x0, y0, x1, y1, color, width: float = 1) -> None:
        self.draw.line(
            (self.s(x0), self.s(y0), self.s(x1), self.s(y1)), fill=self.col(color), width=max(1, self.s(width))
        )

    def dashed(self, x0, y, x1, color, dash: float = 6, gap: float = 5, width: float = 1) -> None:
        x = x0
        while x < x1:
            self.line(x, y, min(x + dash, x1), y, color, width)
            x += dash + gap

    def dotted(self, x0, y, x1, color, step: float = 6, r: float = 0.9) -> None:
        x = x0
        while x <= x1:
            self.draw.ellipse((self.s(x - r), self.s(y - r), self.s(x + r), self.s(y + r)), fill=self.col(color))
            x += step

    def circle(self, cx, cy, r, fill=None, outline=None, width: float = 1) -> None:
        self.draw.ellipse(
            (self.s(cx - r), self.s(cy - r), self.s(cx + r), self.s(cy + r)),
            fill=self.col(fill) if fill is not None else None,
            outline=self.col(outline) if outline is not None else None,
            width=max(1, self.s(width)),
        )

    # Text -------------------------------------------------------------------------------
    def measure(self, text: str, role: str, size: float, tracking: float = 0) -> float:
        if not text:
            return 0.0
        total = 0.0
        for r, chunk in runs(text, role):
            f = load_font(r, self.s(size))
            total += f.getlength(chunk) / self.ss
        return total + tracking * max(0, len(text) - 1)

    def fit(self, text: str, role: str, size: float, max_w: float, tracking: float = 0, middle: bool = False) -> str:
        """Truncate with an ellipsis so the text fits ``max_w``. ``middle`` keeps the tail
        (useful for "AK-47 | Redline (FT)" where the wear matters)."""
        if self.measure(text, role, size, tracking) <= max_w:
            return text
        if middle and len(text) > 8:
            # Keep the informative tail: a trailing "(FN)"-style group, else the last word.
            m = re.search(r"(\([^()]{1,24}\)|\S{1,14})$", text)
            tail = " " + m.group(1) if m else text[-8:]
            while len(tail) > 4 and self.measure(tail, role, size, tracking) > max_w * 0.45:
                tail = tail[1:]
            head = text[: len(text) - len(tail.strip())].rstrip()
            lo, hi = 0, len(head)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self.measure(head[:mid].rstrip(" |-") + ELLIPSIS + tail, role, size, tracking) <= max_w:
                    lo = mid
                else:
                    hi = mid - 1
            if lo >= 3:
                return head[:lo].rstrip(" |-,") + ELLIPSIS + tail
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.measure(text[:mid].rstrip() + ELLIPSIS, role, size, tracking) <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip(" |-,") + ELLIPSIS if lo else ELLIPSIS

    def wrap(self, text: str, role: str, size: float, max_w: float, max_lines: int = 2) -> list[str]:
        words = text.split(" ")
        lines: list[str] = []
        cur = ""
        for i, w in enumerate(words):
            trial = f"{cur} {w}".strip()
            if self.measure(trial, role, size) <= max_w:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) == max_lines - 1:
                rest = " ".join([cur] + words[i + 1 :])
                lines.append(self.fit(rest, role, size, max_w))
                return lines
        if cur:
            lines.append(cur)
        return [self.fit(line, role, size, max_w) for line in lines[:max_lines]]

    def text(
        self,
        xy,
        text: str,
        role: str = "sans",
        size: float = 14,
        fill=None,
        anchor: str = "ls",
        tracking: float = 0,
        max_w: float | None = None,
        middle: bool = False,
    ) -> float:
        """Draw text at a logical point. Anchors: l/m/r horizontally, s (baseline) / m / t vertically.

        Returns the drawn width in logical px.
        """
        if not text:
            return 0.0
        if max_w is not None:
            text = self.fit(text, role, size, max_w, tracking, middle)
        fill = self.col(fill or self.theme.text)
        width = self.measure(text, role, size, tracking)
        x, y = xy
        h_anchor, v_anchor = anchor[0], anchor[1]
        if h_anchor == "m":
            x -= width / 2
        elif h_anchor == "r":
            x -= width
        base_font = load_font(role, self.s(size))
        ascent, descent = base_font.getmetrics()
        if v_anchor == "t":
            y_base = self.s(y) + ascent
        elif v_anchor == "m":
            cap = base_font.getbbox("H", anchor="ls")
            y_base = self.s(y) + (cap[3] - cap[1]) / 2
        else:
            y_base = self.s(y)
        cx = self.s(x)
        for r, chunk in runs(text, role):
            f = load_font(r, self.s(size))
            if tracking:
                for ch in chunk:
                    self.draw.text((cx, y_base), ch, font=f, fill=fill, anchor="ls")
                    cx += f.getlength(ch) + self.s(tracking)
            else:
                self.draw.text((cx, y_base), chunk, font=f, fill=fill, anchor="ls")
                cx += f.getlength(chunk)
        return width

    def label(
        self, xy, text: str, size: float = 11, fill=None, anchor: str = "ls", max_w: float | None = None
    ) -> float:
        """Small uppercase mono label with tracking: the 'terminal' voice of the cards."""
        return self.text(
            xy, text.upper(), "mono_med", size, fill or self.theme.dim, anchor, tracking=size * 0.08, max_w=max_w
        )

    def pill(
        self,
        x,
        y,
        text: str,
        *,
        color=None,
        fill=None,
        size: float = 11,
        role: str = "mono_med",
        pad_x: float = 9,
        h: float = 22,
        anchor: str = "l",
        outline=True,
    ) -> float:
        """A rounded chip. (x, y) is the left (or right) edge and the vertical centre."""
        color = color or self.theme.dim
        w = self.measure(text, role, size, tracking=size * 0.04) + pad_x * 2
        x0 = x - w if anchor == "r" else x
        b = (x0, y - h / 2, x0 + w, y + h / 2)
        if fill is not None:
            self.rrect(b, h / 2, fill=fill)
        if outline:
            self.rrect(b, h / 2, outline=mix(color, self.theme.panel, 0.45), width=1)
        self.text((x0 + pad_x, y), text, role, size, color, anchor="lm", tracking=size * 0.04)
        return w

    # Images -----------------------------------------------------------------------------
    def paste(self, im: Image.Image, b, radius: float = 0) -> None:
        x0, y0, x1, y1 = self.box(b)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        im = im.convert("RGBA").resize((w, h), Image.Resampling.LANCZOS)
        mask = None
        if radius:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), self.s(radius), fill=255)
        self._paste(im, (x0, y0), mask)

    def paste_contain(self, im: Image.Image, b, pad: float = 0) -> None:
        """Fit an icon inside a box keeping its aspect ratio, centred."""
        x0, y0, x1, y1 = b
        bw, bh = (x1 - x0) - pad * 2, (y1 - y0) - pad * 2
        iw, ih = im.size
        scale = min(bw / iw, bh / ih)
        w, h = iw * scale, ih * scale
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.paste(im, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))

    # Output -----------------------------------------------------------------------------
    def finish(self, hidpi: bool = False) -> Image.Image:
        out = self.img
        if not hidpi and self.ss != 1:
            out = out.resize((self.w, self.h), Image.Resampling.LANCZOS)
        flat = Image.new("RGB", out.size, hex_rgb(self.theme.bg_bottom))
        flat.paste(out, mask=out.getchannel("A"))
        return flat
