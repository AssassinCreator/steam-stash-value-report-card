"""Privacy redaction presets for cards, sidecars and terminal output.

none   everything as fetched.
light  name masked, SteamID64 masked to its last 4 digits, avatar blurred, URL and location dropped.
full   like light, but the name becomes "Anonymous collector", the avatar is replaced and totals
       are shown as value bands ("$1k-2.5k") instead of exact figures.

Default: ``none`` for your saved "me" profile, ``full`` for anyone else, ``light`` when no
"me" profile is saved yet. Demo profiles are fictional and default to ``none``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from stashcard.models import Profile
from stashcard.money import CURRENCIES, format_money

LEVELS = ("none", "light", "full")
MASK = "•"  # bullet


def default_level(profile_id: str | None, me: str | None, *, is_demo: bool = False) -> str:
    if is_demo:
        return "none"
    if not me:
        return "light"
    return "none" if profile_id == me else "full"


def at_least(level: str, floor: str) -> str:
    return level if LEVELS.index(level) >= LEVELS.index(floor) else floor


def mask_name(name: str) -> str:
    name = name.strip()
    if len(name) <= 2:
        return MASK * 4
    return name[0] + MASK * min(6, max(3, len(name) - 2)) + name[-1]


def mask_id(steamid64: str) -> str:
    return MASK * 4 + " " + MASK * 4 + " " + steamid64[-4:]


_BANDS = (1, 2.5, 5)


def _num(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1_000:
        return f"{value / 1_000:g}k"
    return f"{value:g}"


def _wrap(body: str, code: str) -> str:
    cur = CURRENCIES.get(code)
    if cur is None:
        return f"{body} {code}"
    return f"{cur.symbol}{body}" if cur.prefix else f"{body}{cur.symbol}"


def value_band(minor: int, code: str) -> str:
    """Round a total into a coarse 1-2.5-5 band: 183400 USD -> "$1k-2.5k"."""
    major = max(minor, 0) / 100
    if major < 1:
        return "< " + _wrap("1", code)
    lo, scale = 1.0, 1.0
    while True:
        for step in _BANDS:
            hi = step * scale
            if hi > major:
                return _wrap(f"{_num(lo)}\u2013{_num(hi)}", code)
            lo = hi
        scale *= 10


@dataclass(frozen=True)
class Redaction:
    level: str = "none"

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f"Redaction must be one of: {', '.join(LEVELS)}")

    @property
    def active(self) -> bool:
        return self.level != "none"

    @property
    def bands(self) -> bool:
        return self.level == "full"

    def name(self, profile: Profile) -> str:
        if self.level == "full":
            return "Anonymous collector"
        if self.level == "light":
            return mask_name(profile.persona)
        return profile.persona

    def steamid(self, profile: Profile) -> str:
        return profile.steamid64 if self.level == "none" else mask_id(profile.steamid64)

    def url(self, profile: Profile) -> str | None:
        return profile.url if self.level == "none" else None

    def total(self, minor: int | None, code: str, **kw) -> str:
        if minor is None:
            return "n/a"
        if self.bands:
            return value_band(minor, code)
        return format_money(minor, code, **kw)

    def profile(self, profile: Profile) -> Profile:
        """A copy safe to serialise into a sidecar."""
        if self.level == "none":
            return profile
        return replace(
            profile,
            persona=self.name(profile),
            steamid64=self.steamid(profile),
            vanity=None,
            avatar_url=None,
            location=None,
            member_since=profile.member_since if self.level == "light" else None,
        )
