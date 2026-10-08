"""Card themes. Each theme is a small set of tokens; layouts never hard-code colours."""

from __future__ import annotations

from dataclasses import dataclass


def hex_rgb(value: str, alpha: int | None = None) -> tuple[int, ...]:
    v = value.lstrip("#")
    rgb = tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))
    return rgb if alpha is None else (*rgb, alpha)


def mix(a: str | tuple, b: str | tuple, t: float) -> tuple[int, int, int]:
    ca = hex_rgb(a) if isinstance(a, str) else a[:3]
    cb = hex_rgb(b) if isinstance(b, str) else b[:3]
    return tuple(int(round(ca[i] + (cb[i] - ca[i]) * t)) for i in range(3))


def luminance(c: str | tuple) -> float:
    r, g, b = hex_rgb(c) if isinstance(c, str) else c[:3]

    def ch(x: int) -> float:
        x = x / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str | tuple, b: str | tuple) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@dataclass(frozen=True)
class Theme:
    name: str
    title: str
    dark: bool
    bg_top: str
    bg_bottom: str
    panel: str
    panel_hi: str  # top of the panel gradient
    border: str
    text: str
    dim: str
    faint: str
    accent: str  # the brand colour on this theme
    accent2: str
    good: str
    bad: str
    warn: str
    palette: tuple[str, ...]  # categorical colours for games
    rule: str  # hairlines and dashed rules
    paper: str = "#F7F3EA"  # receipt paper
    ink: str = "#1C1B19"
    grain: float = 0.0  # paper noise strength 0..1
    scanlines: bool = False
    glow: bool = True
    mono_names: bool = False  # set item names in mono (phosphor)

    def game_color(self, slot: int) -> str:
        return self.palette[slot % len(self.palette)]


THEMES: dict[str, Theme] = {
    "midnight": Theme(
        name="midnight",
        title="Midnight",
        dark=True,
        bg_top="#0B0F17",
        bg_bottom="#0F1626",
        panel="#111827",
        panel_hi="#151E2F",
        border="#223047",
        text="#EAF0F8",
        dim="#93A1B5",
        faint="#56637A",
        accent="#7CF0B6",
        accent2="#F5B94A",
        good="#6EE7A8",
        bad="#FF7A7A",
        warn="#F5B94A",
        palette=("#F5B94A", "#FF7A7A", "#C9A2FF", "#7DD3FC", "#7CF0B6", "#F0ABFC"),
        rule="#2A3950",
    ),
    "paper": Theme(
        name="paper",
        title="Paper",
        dark=False,
        bg_top="#F4F0E6",
        bg_bottom="#ECE6D8",
        panel="#FBF8F1",
        panel_hi="#FFFDF8",
        border="#D8CFBE",
        text="#1B1A17",
        dim="#5F594E",
        faint="#9A9284",
        accent="#D23F2A",
        accent2="#1D4ED8",
        good="#1E7A46",
        bad="#C2321F",
        warn="#A86A12",
        palette=("#D23F2A", "#1D4ED8", "#A86A12", "#1E7A46", "#7A4FB5", "#0E7C86"),
        rule="#CFC5B2",
        grain=0.55,
        glow=False,
    ),
    "phosphor": Theme(
        name="phosphor",
        title="Phosphor",
        dark=True,
        bg_top="#030703",
        bg_bottom="#061006",
        panel="#071107",
        panel_hi="#0A180B",
        border="#174D22",
        text="#B9FFC9",
        dim="#62B676",
        faint="#2F6B3C",
        accent="#3DFF7F",
        accent2="#FFB23E",
        good="#3DFF7F",
        bad="#FF6E5A",
        warn="#FFB23E",
        palette=("#3DFF7F", "#FFB23E", "#7FE8FF", "#E8FF7A", "#2FBF62", "#FF8BD0"),
        rule="#1B5A28",
        paper="#E9F7EA",
        ink="#0B2A12",
        scanlines=True,
        mono_names=True,
    ),
}

DEFAULT_THEME = "midnight"


def get_theme(name: str | None) -> Theme:
    key = (name or DEFAULT_THEME).lower()
    if key not in THEMES:
        raise ValueError(f"Unknown theme {name!r}. Available: {', '.join(THEMES)}")
    return THEMES[key]
