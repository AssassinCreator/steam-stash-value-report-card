"""Supported games (Steam inventory app/context pairs)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    key: str
    name: str
    short: str
    appid: int
    contextid: int
    skinport: bool = False  # the Skinport public API covers this app
    publisher_fee: float = 0.10
    slot: int = 5  # index into the theme's categorical palette

    @property
    def context(self) -> str:
        return f"{self.appid}/{self.contextid}"

    @property
    def custom(self) -> bool:
        return self.key.startswith("app")


GAMES: dict[str, Game] = {
    "cs2": Game("cs2", "Counter-Strike 2", "CS2", 730, 2, True, slot=0),
    "dota2": Game("dota2", "Dota 2", "Dota 2", 570, 2, True, slot=1),
    "tf2": Game("tf2", "Team Fortress 2", "TF2", 440, 2, True, slot=2),
    "rust": Game("rust", "Rust", "Rust", 252490, 2, True, slot=3),
    "steam": Game("steam", "Steam Community items", "Steam", 753, 6, False, slot=4),
}

ALIASES = {
    "csgo": "cs2",
    "cs": "cs2",
    "counter-strike": "cs2",
    "730": "cs2",
    "dota": "dota2",
    "570": "dota2",
    "tf": "tf2",
    "440": "tf2",
    "252490": "rust",
    "753": "steam",
    "community": "steam",
    "cards": "steam",
    "steam-items": "steam",
}

DEFAULT_GAMES = ("cs2", "dota2", "steam")

_CUSTOM = re.compile(r"^(?:app)?(\d{1,10})(?:[/:](\d{1,20}))?$")


def by_appid(appid: int) -> Game | None:
    for g in GAMES.values():
        if g.appid == appid:
            return g
    return None


def parse_game(token: str) -> Game:
    """Resolve ``cs2``, ``dota``, ``440`` or a custom ``appid/contextid`` to a Game."""
    t = token.strip().lower()
    t = ALIASES.get(t, t)
    if t in GAMES:
        return GAMES[t]
    m = _CUSTOM.match(t)
    if m:
        appid = int(m.group(1))
        ctx = int(m.group(2) or 2)
        known = by_appid(appid)
        if known and known.contextid == ctx:
            return known
        return Game(f"app{appid}_{ctx}", f"App {appid}", f"App {appid}", appid, ctx, False, slot=5)
    names = ", ".join(GAMES)
    raise ValueError(f"Unknown game {token!r}. Use one of: {names}, or a custom appid/contextid like 252490/2.")


def parse_games(tokens: list[str] | tuple[str, ...] | str | None) -> list[Game]:
    if not tokens:
        tokens = list(DEFAULT_GAMES)
    if isinstance(tokens, str):
        tokens = [tokens]
    out: list[Game] = []
    for tok in tokens:
        for part in str(tok).split(","):
            if part.strip():
                g = parse_game(part)
                if g not in out:
                    out.append(g)
    return out
