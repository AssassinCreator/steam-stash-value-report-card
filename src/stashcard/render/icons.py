"""Item icons (downloaded once, cached forever), offline placeholders and avatars."""

from __future__ import annotations

import hashlib
import io
import re

from PIL import Image, ImageDraw, ImageFilter

from stashcard.models import Item
from stashcard.render.canvas import load_font
from stashcard.render.theme import Theme, hex_rgb, mix

NEUTRAL = "8B98AA"


def accent_of(item: Item, theme: Theme) -> str:
    c = item.accent
    return f"#{c}" if c else (theme.dim if theme.dark else theme.faint)


def monogram(item: Item) -> str:
    base = item.market_hash_name
    if item.game.appid == 753 and "-" in base.split(" ")[0]:
        base = item.name or base.split("-", 1)[1]
    base = base.replace("★", "").replace("StatTrak™", "").replace("Souvenir", "")
    base = base.split("|")[0].strip()
    base = re.sub(r"\(.*?\)", "", base).strip(" :")
    words = [w for w in re.split(r"\s+", base) if w and w not in ("-", "&")]
    if not words:
        return "?"
    if len(words) == 1:
        clean = re.sub(r"[^A-Za-z0-9]", "", words[0])
        return (clean[:4] or "?").upper()
    letters = "".join(w[0] for w in words if w[0].isalnum())
    return (letters[:3] or "?").upper()


def placeholder(item: Item, theme: Theme, size: int = 256) -> Image.Image:
    """A deliberate-looking offline tile: rarity-tinted field, corner brackets, a monogram."""
    accent = accent_of(item, theme)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    base = mix(accent, theme.panel, 0.82 if theme.dark else 0.88)
    # soft radial field
    field = Image.new("RGBA", (size, size), (*base, 0))
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((size * 0.08, size * 0.08, size * 0.92, size * 0.92), fill=200)
    m = m.filter(ImageFilter.GaussianBlur(size * 0.12))
    field.putalpha(m)
    img.alpha_composite(field)
    ink = hex_rgb(accent, 230) if theme.dark else (*mix(accent, theme.text, 0.45), 235)
    br, ln, w = size * 0.18, size * 0.12, max(2, size // 64)
    for x, y, dx, dy in (
        (br, br, 1, 1),
        (size - br, br, -1, 1),
        (br, size - br, 1, -1),
        (size - br, size - br, -1, -1),
    ):
        d.line((x, y, x + dx * ln, y), fill=ink, width=w)
        d.line((x, y, x, y + dy * ln), fill=ink, width=w)
    if not item.marketable:
        # medals, coins, gems: a ring
        r = size * 0.2
        d.ellipse(
            (size / 2 - r, size / 2 - r - size * 0.04, size / 2 + r, size / 2 + r - size * 0.04),
            outline=ink,
            width=max(2, size // 48),
        )
    text = monogram(item)
    font = load_font("mono_bold", int(size * (0.2 if len(text) <= 3 else 0.17)))
    y = size * (0.62 if not item.marketable else 0.5)
    d.text((size / 2, y), text, font=font, fill=ink, anchor="mm")
    return img


def load_icon(backend, item: Item, theme: Theme, size: int = 256) -> tuple[Image.Image, bool]:
    """(image, is_real). Falls back to a placeholder when offline or the icon is missing."""
    if backend is not None and item.icon_url:
        try:
            data = backend.icon(item.icon_url, size)
        except Exception:  # never let a bad icon break a card
            data = None
        if data:
            try:
                im = Image.open(io.BytesIO(data))
                im.load()
                return im.convert("RGBA"), True
            except Exception:
                pass
    return placeholder(item, theme, size), False


def identicon(seed: str, theme: Theme, size: int = 256) -> Image.Image:
    """5x5 mirrored pixel avatar from a seed; used for demo profiles and missing avatars."""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    fg = theme.palette[h[0] % len(theme.palette)]
    bg = mix(fg, theme.panel, 0.86 if theme.dark else 0.9)
    img = Image.new("RGBA", (size, size), (*bg, 255))
    d = ImageDraw.Draw(img)
    cell = size / 7
    for row in range(5):
        for col in range(3):
            if (h[1 + row * 3 + col] & 1) == 1:
                for c in {col, 4 - col}:
                    x0, y0 = cell * (c + 1), cell * (row + 1)
                    d.rectangle((x0, y0, x0 + cell - 1, y0 + cell - 1), fill=hex_rgb(fg, 255))
    return img


def anonymous_avatar(theme: Theme, size: int = 256) -> Image.Image:
    bg = mix(theme.dim, theme.panel, 0.8 if theme.dark else 0.85)
    img = Image.new("RGBA", (size, size), (*bg, 255))
    d = ImageDraw.Draw(img)
    ink = hex_rgb(theme.dim, 255)
    for i in range(-size, size * 2, max(6, size // 14)):
        d.line((i, 0, i - size, size), fill=(*ink[:3], 40), width=max(1, size // 64))
    d.ellipse((size * 0.34, size * 0.2, size * 0.66, size * 0.52), fill=ink)
    d.rounded_rectangle((size * 0.2, size * 0.58, size * 0.8, size * 1.05), radius=size * 0.3, fill=ink)
    return img


def avatar_image(backend, profile, theme: Theme, redaction: str = "none", size: int = 256) -> Image.Image:
    if redaction == "full":
        return anonymous_avatar(theme, size)
    im = None
    if not profile.is_demo and backend is not None and profile.avatar_url:
        try:
            data = backend.avatar(profile.avatar_url)
            if data:
                im = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            im = None
    if im is None:
        im = identicon(profile.steamid64 if redaction == "none" else profile.steamid64[-4:], theme, size)
    if redaction == "light":
        small = im.resize((8, 8), Image.Resampling.BILINEAR)
        im = small.resize((size, size), Image.Resampling.NEAREST).filter(ImageFilter.GaussianBlur(size / 18))
    return im
