"""The offline demo: two fictional profiles with real item names, classids and icon hashes.

The demo goes through the exact same pipeline as a live scan (inventory join, pricing chain,
fallback labels, warnings, rendering). Only the backend is different: it reads a bundled
fixture instead of the network. Prices are a dated snapshot, converted with fixed demo FX
rates for non-USD currencies, and every card says DEMO.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources

from stashcard.cache import Hit
from stashcard.fx import FxTable
from stashcard.games import Game
from stashcard.markets import VENUES
from stashcard.models import Profile
from stashcard.money import CURRENCIES
from stashcard.net import NotFoundError
from stashcard.profile import ProfileRef
from stashcard.sources import InventoryResult

DEMO_REFS = ("demo", "demo2", "demo@then", "demo2@then")


@lru_cache(maxsize=1)
def fixture() -> dict:
    with resources.files("stashcard.data").joinpath("demo.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def is_demo_ref(text: str) -> bool:
    return text.strip().lower() in DEMO_REFS


def split_ref(text: str) -> tuple[str, str]:
    """'demo@then' -> ('demo', 'then'); 'demo2' -> ('demo2', 'now')."""
    name, _, epoch = text.strip().lower().partition("@")
    return name, epoch or "now"


class DemoBackend:
    is_demo = True

    def __init__(self, name: str = "demo", epoch: str = "now", icons=None):
        data = fixture()
        if name not in data["profiles"]:
            raise NotFoundError(f"No demo profile called {name!r}. Try demo or demo2.")
        self.name = name
        self.epoch = epoch
        self.data = data
        self.blob = data["profiles"][name]
        self.icons = icons  # optional LiveBackend used only to fetch item icons
        # STASHCARD_DEMO_PACE=0.5 adds network-like latency so progress is watchable (README GIF).
        try:
            self.pace = max(0.0, float(os.environ.get("STASHCARD_DEMO_PACE", "0") or 0))
        except ValueError:
            self.pace = 0.0
        if epoch == "then":
            then = self.blob["then"]
            self.inventories = then["inventories"]
            self.factor = then.get("factor", {})
            self.default_factor = float(then.get("default_factor", 1.0))
            self.as_of = datetime.fromisoformat(then["date"]).replace(hour=12, tzinfo=timezone.utc)
        else:
            self.inventories = self.blob["inventories"]
            self.factor = {}
            self.default_factor = 1.0
            self.as_of = datetime.fromisoformat(data["snapshot"]).replace(hour=12, tzinfo=timezone.utc)

    @property
    def label(self) -> str | None:
        return "then" if self.epoch == "then" else None

    # Helpers ------------------------------------------------------------------------------
    def _fx(self, usd_minor: int | None, currency: str, name: str) -> int | None:
        if usd_minor is None:
            return None
        rate = float(self.data["fx"].get(currency, 0)) or None
        if rate is None:
            return None
        factor = self.factor.get(name, self.default_factor)
        return max(1, round(usd_minor * rate * factor))

    def _price(self, game: Game, name: str) -> dict:
        return self.data["prices"].get(f"{game.appid}|{name}") or {}

    def _hit(self) -> Hit:
        return Hit(None, 0.0)

    def _wait(self, weight: float) -> None:
        if self.pace:
            time.sleep(self.pace * weight)

    # Backend interface --------------------------------------------------------------------
    def profile(self, ref: ProfileRef | None = None) -> Profile:
        p = dict(self.blob["profile"])
        return Profile(**p, is_demo=True)

    def inventory(self, steamid64: str, game: Game) -> InventoryResult:
        self._wait(0.7)
        raw = self.inventories.get(game.context)
        if raw is None:
            return InventoryResult({"assets": [], "descriptions": [], "total_inventory_count": 0, "success": 1})
        return InventoryResult(raw)

    def steam_price(self, game: Game, name: str, currency: str):
        if not CURRENCIES[currency].steam_id:
            return None, None
        self._wait(0.05)
        st = self._price(game, name).get("steam")
        if not st:
            return None, self._hit()
        return {
            "lowest": self._fx(st.get("lowest"), currency, name),
            "median_24h": self._fx(st.get("median_24h"), currency, name),
            "volume_24h": st.get("volume_24h"),
        }, self._hit()

    def steam_history(self, game: Game, name: str, currency: str):
        return None  # the demo has no login cookie, like most users

    def _skinport_rows(self, game: Game, currency: str):
        self._wait(0.25)
        if not game.skinport or not CURRENCIES[currency].skinport:
            return None
        prefix = f"{game.appid}|"
        return {
            k[len(prefix) :]: v["skinport"]
            for k, v in self.data["prices"].items()
            if k.startswith(prefix) and v.get("skinport")
        }

    def skinport_items(self, game: Game, currency: str):
        rows = self._skinport_rows(game, currency)
        if rows is None:
            return None, None
        return {
            n: {"min": self._fx(sp.get("min"), currency, n), "quantity": sp.get("quantity", 0)}
            for n, sp in rows.items()
        }, self._hit()

    def skinport_history(self, game: Game, currency: str):
        rows = self._skinport_rows(game, currency)
        if rows is None:
            return None, None
        out = {}
        for n, sp in rows.items():
            out[n] = {}
            for win, h in (sp.get("history") or {}).items():
                if not h.get("volume") or h.get("avg") is None:
                    continue
                out[n][win] = {
                    k: (self._fx(h.get(k), currency, n) if k != "volume" else h["volume"])
                    for k in ("avg", "median", "max", "min", "volume")
                }
        return out, self._hit()

    # Third-party venues (USD, like the live feeds) -----------------------------------------
    def _usd(self, usd_minor: int, name: str) -> int:
        return max(1, round(usd_minor * self.factor.get(name, self.default_factor)))

    def venue_catalog(self, venue: str, game: Game):
        if not VENUES[venue].supports(game.appid):
            return None, None
        self._wait(0.25)
        prefix = f"{game.appid}|"
        out = {}
        for key, entry in self.data["prices"].items():
            row = (entry.get("venues") or {}).get(venue)
            if key.startswith(prefix) and row:
                name = key[len(prefix) :]
                out[name] = {"usd": self._usd(row["usd"], name), "count": row.get("count", 0)}
        return out, self._hit()

    def fx_table(self):
        source = self.data.get("fx_source", "demo fixture")
        return FxTable(
            "USD", dict(self.data["fx"]), self.data.get("fx_date", self.data["snapshot"]), source
        ), self._hit()

    def csfloat_history(self, name: str):
        self._wait(0.08)
        rows = (self.data["prices"].get(f"730|{name}") or {}).get("csfloat_history") or []
        f = self.factor.get(name, self.default_factor)
        return [[day, round(avg * f), count] for day, avg, count in rows]

    def icon(self, icon_url: str, size: int = 256) -> bytes | None:
        if self.icons is None:
            return None
        return self.icons.icon(icon_url, size)

    def avatar(self, url: str | None) -> bytes | None:
        return None
