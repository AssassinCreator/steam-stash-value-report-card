"""Price resolution under the owner's contract.

LOWEST  = the current floor listing.
AVG     = the mean of actual sales over a window (24h / 7d / 30d / 90d).

Every number is a Quote that carries its source (venue) and window. When the requested metric
is not available we fall back, and the Quote is marked ``fallback=True`` so the label says so.
Nothing is ever silently zero: no data means no Quote, and the item is counted as unpriced.

The AVG stays the mean (the contract), but the window median and max ride along so an
outlier-pulled mean is badged "skewed". ``trimmed_avg`` is opt-in and always labelled.

Marketplaces: Steam (wallet funds) plus cash venues (Skinport, Waxpeer, Market.CSGO,
Lis-skins; CSFloat history as an experimental second AVG). Third-party USD quotes are converted
with a dated FX rate that rides along on every converted number.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from stashcard.fx import FxRate
from stashcard.games import Game
from stashcard.markets import DEFAULT_MARKETS, FLOOR_FEEDS, VENUES, csfloat_stats
from stashcard.models import WINDOW_DAYS, Item, PricedItem, Quote, VenueQuote
from stashcard.money import CURRENCIES, Wallet, seller_receives
from stashcard.net import FetchError, OfflineError, RateLimited
from stashcard.sources import history_stats, trimmed_from_summary

SOURCES = ("both", "steam", "skinport")
SOURCE_HELP = {
    "both": "LOWEST from Steam Market, AVG from Skinport sales (recommended)",
    "steam": "Steam Market only (AVG needs your login cookie, else 24h median)",
    "skinport": "Skinport only (floor and sales average)",
}
DEFAULT_SKINPORT_FEE = VENUES["skinport"].fee  # TODO(verify): see markets.py
DEFAULT_CAPTURE = 0.10  # share of daily market volume one seller can realistically take
LABEL = {"steam": "Steam"}  # short source names on labels; others use the venue name


def venue_label(key: str) -> str:
    return LABEL.get(key, VENUES[key].name if key in VENUES else key)


@dataclass
class ScanOptions:
    games: list[Game]
    currency: str = "USD"
    source: str = "both"
    window: str = "7d"
    min_price: int = 0  # minor units
    tradable_only: bool = False
    trimmed_avg: bool = False
    capture: float = DEFAULT_CAPTURE
    skinport_fee: float = DEFAULT_SKINPORT_FEE
    steam_budget: int = 60  # max live Steam price lookups per scan; cache hits are free
    markets: list[str] = field(default_factory=lambda: list(DEFAULT_MARKETS))
    lowest_from: str | None = None  # None = from --source (steam, or skinport); "best"; or a venue key
    fees: dict[str, float] = field(default_factory=dict)  # seller fee overrides per venue
    csfloat_top: int = 10  # CSFloat history lookups per scan (top items by value)

    @property
    def primary_lowest(self) -> str:
        if self.lowest_from:
            return self.lowest_from
        return "skinport" if self.source == "skinport" else "steam"

    def fee_for(self, venue: str) -> float | None:
        if venue == "steam":
            return None
        if venue in self.fees:
            return self.fees[venue]
        if venue == "skinport":
            return self.skinport_fee
        return VENUES[venue].fee

    def enabled(self, venue: str) -> bool:
        return venue in self.markets

    def validate(self) -> None:
        if self.currency not in CURRENCIES:
            raise ValueError(f"Unsupported currency {self.currency}")
        if self.source not in SOURCES:
            raise ValueError(f"Source must be one of {', '.join(SOURCES)}")
        if self.window not in WINDOW_DAYS:
            raise ValueError("Window must be 24h, 7d, 30d or 90d")
        if not 0 < self.capture <= 1:
            raise ValueError("Capture rate must be between 0 and 1 (for example 0.1 for 10 %)")
        if not 0 <= self.skinport_fee < 1:
            raise ValueError("Skinport fee must be between 0 and 1 (for example 0.08 for 8 %)")
        unknown = [m for m in self.markets if m not in VENUES]
        if unknown:
            raise ValueError(f"Unknown marketplace(s): {', '.join(unknown)}")
        floors = [k for k, v in VENUES.items() if v.kind == "floor"]
        if self.lowest_from and self.lowest_from not in ("best", *floors):
            raise ValueError(f"--lowest-from must be steam, best, or one of: {', '.join(floors)}")
        if self.lowest_from and self.lowest_from not in ("best",) and self.lowest_from not in self.markets:
            self.markets.append(self.lowest_from)

    def to_dict(self) -> dict:
        return {
            "games": [g.key if not g.custom else g.context for g in self.games],
            "currency": self.currency,
            "source": self.source,
            "window": self.window,
            "min_price": self.min_price,
            "tradable_only": self.tradable_only,
            "trimmed_avg": self.trimmed_avg,
            "capture": self.capture,
            "skinport_fee": self.skinport_fee,
            "markets": list(self.markets),
            "lowest_from": self.primary_lowest,
            "fees": {m: self.fee_for(m) for m in self.markets if m != "steam"},
        }


@dataclass
class Notice:
    kind: str  # ratelimit | cache | source | info
    text: str


@dataclass
class PricingStats:
    steam_live: int = 0
    steam_cached: int = 0
    steam_skipped: int = 0  # not attempted: budget exhausted or rate-limited
    steam_rate_limited: bool = False
    skinport_apps: list[str] = field(default_factory=list)
    venue_apps: dict[str, list[str]] = field(default_factory=dict)
    csfloat_lookups: int = 0


Progress = Callable[[str, int, int], None]  # (label, done, total)


class PriceEngine:
    def __init__(self, backend, opts: ScanOptions, progress: Progress | None = None):
        self.backend = backend
        self.opts = opts
        self.progress = progress or (lambda label, done, total: None)
        self.notices: list[Notice] = []
        self.stats = PricingStats()
        self.fx: FxRate | None = None  # USD -> report currency, for USD-only venues
        self._sp_items: dict[int, dict | None] = {}
        self._sp_hist: dict[int, dict | None] = {}
        self._feeds: dict[str, dict[int, dict]] = {}

    # Skinport ---------------------------------------------------------------------------
    def _load_skinport(self, games: list[Game]) -> None:
        if not (self.opts.enabled("skinport") or self.opts.source == "skinport"):
            return
        cur = CURRENCIES[self.opts.currency]
        sp_games = [g for g in games if g.skinport]
        if sp_games and not cur.skinport:
            self.notices.append(Notice("source", f"Skinport does not quote {cur.code}; AVG falls back to Steam."))
            return
        for n, game in enumerate(sp_games):
            self.progress(f"Skinport {game.short}", n, len(sp_games))
            for kind, store, fn in (
                ("catalog", self._sp_items, self.backend.skinport_items),
                ("sales history", self._sp_hist, self.backend.skinport_history),
            ):
                try:
                    data, hit = fn(game, cur.code)
                except RateLimited as exc:
                    self.notices.append(Notice("ratelimit", f"Skinport {kind} for {game.short}: {exc}"))
                    data, hit = None, None
                except (FetchError, OfflineError) as exc:
                    self.notices.append(Notice("source", f"Skinport {kind} for {game.short} unavailable: {exc}"))
                    data, hit = None, None
                store[game.appid] = data
                if hit is not None and hit.age > 3600 * 1.5:
                    self.notices.append(
                        Notice("cache", f"Skinport {kind} for {game.short} is from cache ({hit.age_text} old).")
                    )
            if self._sp_items.get(game.appid) is not None:
                self.stats.skinport_apps.append(game.short)
        if sp_games:
            self.progress("Skinport", len(sp_games), len(sp_games))

    # Third-party floors -----------------------------------------------------------------
    def _load_fx(self) -> bool:
        cur = self.opts.currency
        if cur == "USD":
            self.fx = FxRate("USD", "USD", 1.0, "", "")
            return True
        try:
            table, hit = self.backend.fx_table()
        except (RateLimited, FetchError, OfflineError) as exc:
            table, hit = None, None
            self.notices.append(Notice("source", f"No FX rate for USD→{cur} ({exc})."))
        rate = table.rate(cur) if table else None
        if rate is None:
            self.notices.append(
                Notice(
                    "source",
                    f"Third-party venues quote USD only and no USD→{cur} rate is available; "
                    "they are skipped. Use -c USD to include them.",
                )
            )
            return False
        self.fx = rate
        if hit is not None and hit.age > 24 * 3600:
            self.notices.append(Notice("cache", f"FX rate is from cache ({hit.age_text} old): {rate.label}."))
        return True

    def _load_feeds(self, games: list[Game]) -> None:
        feeds = [v for v in FLOOR_FEEDS if self.opts.enabled(v)]
        wants_csfloat = self.opts.enabled("csfloat") and any(g.appid == 730 for g in games)
        if not feeds and not wants_csfloat:
            return
        if not self._load_fx():
            return
        jobs = [(v, g) for v in feeds for g in games if VENUES[v].supports(g.appid)]
        for n, (venue, game) in enumerate(jobs):
            name = VENUES[venue].name
            self.progress(f"{name} {game.short}", n, len(jobs))
            try:
                data, hit = self.backend.venue_catalog(venue, game)
            except RateLimited as exc:
                self.notices.append(Notice("ratelimit", f"{name} catalog for {game.short}: {exc}"))
                continue
            except (FetchError, OfflineError, ValueError) as exc:
                self.notices.append(Notice("source", f"{name} catalog for {game.short} unavailable: {exc}"))
                continue
            if data is None:
                continue
            self._feeds.setdefault(venue, {})[game.appid] = data
            self.stats.venue_apps.setdefault(venue, []).append(game.short)
            if hit is not None and hit.age > 3600 * 1.5:
                self.notices.append(
                    Notice("cache", f"{name} catalog for {game.short} is from cache " f"({hit.age_text} old).")
                )
        if jobs:
            self.progress("Marketplaces", len(jobs), len(jobs))

    def _feed_rows(self, item: Item) -> dict[str, dict]:
        out = {}
        for venue, by_app in self._feeds.items():
            row = (by_app.get(item.game.appid) or {}).get(item.market_hash_name)
            if row and row.get("usd"):
                out[venue] = row
        return out

    # Skinport lookups -------------------------------------------------------------------
    def _sp(self, item: Item) -> tuple[int | None, int | None, dict]:
        cat = self._sp_items.get(item.game.appid) or {}
        hist = self._sp_hist.get(item.game.appid) or {}
        row = cat.get(item.market_hash_name) or {}
        win = (hist.get(item.market_hash_name) or {}).get(self.opts.window) or {}
        return row.get("min"), row.get("quantity"), win

    def _steam_wanted(self, sp_min: int | None, sp_avg: int | None) -> bool:
        if not self.opts.enabled("steam") or not CURRENCIES[self.opts.currency].steam_id:
            return False
        if self.opts.source in ("both", "steam") or self.opts.primary_lowest != "skinport":
            return True
        return sp_min is None or sp_avg is None  # skinport mode: only to fill gaps

    def _wants_history(self, sp_win: dict) -> bool:
        return self.opts.source == "steam" or not sp_win.get("avg")

    # Main -------------------------------------------------------------------------------
    def price(self, items: list[Item]) -> list[PricedItem]:
        games: list[Game] = []
        for it in items:
            if it.game not in games:
                games.append(it.game)
        self._load_skinport(games)
        self._load_feeds(games)
        if self.opts.enabled("steam") and not CURRENCIES[self.opts.currency].steam_id:
            self.notices.append(Notice("source", f"Steam Market does not quote {self.opts.currency}; using Skinport."))

        cur, window = self.opts.currency, self.opts.window
        prepared = [(it, *self._sp(it), self._feed_rows(it)) for it in items]

        def estimate(i: int) -> int:
            it, sp_min, _q, sp_win, feeds = prepared[i]
            third = min((r["usd"] for r in feeds.values()), default=0)
            return (sp_min or sp_win.get("avg") or third or 0) * it.quantity

        # Ask Steam about the most valuable items first so a budget or a 429 hurts least.
        order = sorted(range(len(prepared)), key=estimate, reverse=True)
        wanted = [i for i in order if self._steam_wanted(prepared[i][1], prepared[i][3].get("avg"))]
        steam: dict[int, dict | None] = {}
        history: dict[int, dict | None] = {}
        total = len(wanted)
        for n, i in enumerate(wanted):
            it = prepared[i][0]
            self.progress(f"Steam Market {it.game.short}", n, total)
            if self.stats.steam_rate_limited or self.stats.steam_live >= self.opts.steam_budget:
                self.stats.steam_skipped += 1
                continue
            try:
                data, hit = self.backend.steam_price(it.game, it.market_hash_name, cur)
                if hit is None:
                    self.stats.steam_live += 1
                else:
                    self.stats.steam_cached += 1
                steam[i] = data
                if self._wants_history(prepared[i][3]):
                    rows = self.backend.steam_history(it.game, it.market_hash_name, cur)
                    if rows is not None:
                        history[i] = history_stats(rows, window)
            except RateLimited:
                self.stats.steam_rate_limited = True
                self.stats.steam_skipped += 1
            except (FetchError, OfflineError):
                self.stats.steam_skipped += 1
        if total:
            self.progress("Steam Market", total, total)
        self._steam_notices()

        csfloat = self._load_csfloat(prepared, order)
        return [
            self.compose(
                it, sp_min, sp_win, steam.get(i), history.get(i), sp_qty=sp_qty, feeds=feeds, csfloat=csfloat.get(i)
            )
            for i, (it, sp_min, sp_qty, sp_win, feeds) in enumerate(prepared)
        ]

    def _steam_notices(self) -> None:
        if self.stats.steam_rate_limited:
            self.notices.append(
                Notice(
                    "ratelimit",
                    f"Steam rate-limited price lookups; {self.stats.steam_skipped} item(s) use another venue or stay "
                    "unpriced. Retry later: answers so far are cached.",
                )
            )
        elif self.stats.steam_skipped and self.stats.steam_live >= self.opts.steam_budget:
            self.notices.append(
                Notice(
                    "info",
                    f"Steam lookups capped at {self.opts.steam_budget} per scan (be kind to Steam); "
                    f"{self.stats.steam_skipped} cheaper item(s) use other venues. Run again to extend from cache.",
                )
            )

    def _load_csfloat(self, prepared, order) -> dict[int, dict]:
        """Experimental: CSFloat daily sales averages for the top CS2 items (opt-in)."""
        out: dict[int, dict] = {}
        if not self.opts.enabled("csfloat") or self.fx is None:
            return out
        now = getattr(self.backend, "as_of", None) or datetime.now(timezone.utc)
        targets = [i for i in order if prepared[i][0].game.appid == 730][: max(0, self.opts.csfloat_top)]
        for n, i in enumerate(targets):
            it = prepared[i][0]
            self.progress("CSFloat history", n, len(targets))
            try:
                rows = self.backend.csfloat_history(it.market_hash_name)
            except RateLimited as exc:
                self.notices.append(Notice("ratelimit", f"CSFloat history: {exc}"))
                break
            except (FetchError, OfflineError) as exc:
                self.notices.append(Notice("source", f"CSFloat history unavailable: {exc}"))
                break
            self.stats.csfloat_lookups += 1
            stats = csfloat_stats(rows or [], WINDOW_DAYS[self.opts.window], now)
            if stats:
                out[i] = stats
        if targets:
            self.progress("CSFloat history", len(targets), len(targets))
        return out

    # Compose ----------------------------------------------------------------------------
    def compose(
        self,
        it: Item,
        sp_min: int | None,
        sp_win: dict,
        st: dict | None,
        hist: dict | None,
        *,
        sp_qty: int | None = None,
        feeds: dict | None = None,
        csfloat: dict | None = None,
    ) -> PricedItem:
        cur, window, src = self.opts.currency, self.opts.window, self.opts.source
        st = st or {}
        hist = hist or {}
        feeds = feeds or {}
        trimmed = self.opts.trimmed_avg
        fx = self.fx
        fx_label = fx.short if fx is not None and not fx.identity else None

        # Floors at every venue -------------------------------------------------------------
        venues: dict[str, VenueQuote] = {}
        if st.get("lowest"):
            net = seller_receives(st["lowest"], Wallet(publisher_fee=it.game.publisher_fee))
            venues["steam"] = VenueQuote("steam", st["lowest"], net, wallet=True, fee=None)
        if sp_min:
            fee = self.opts.fee_for("skinport")
            venues["skinport"] = VenueQuote("skinport", sp_min, math.floor(sp_min * (1 - fee)), fee=fee, count=sp_qty)
        for venue, row in feeds.items():
            amount = fx.convert(row["usd"]) if fx else None
            if not amount:
                continue
            fee = self.opts.fee_for(venue)
            venues[venue] = VenueQuote(
                venue,
                amount,
                math.floor(amount * (1 - fee)),
                fee=fee,
                count=row.get("count"),
                native=row["usd"],
                fx=fx_label,
            )

        def floor_quote(key: str) -> Quote | None:
            v = venues.get(key)
            if v is None:
                return None
            return Quote(v.amount, cur, venue_label(key), "floor", "now", volume=v.count, fx=v.fx, venue=key)

        primary = self.opts.primary_lowest
        if primary == "best":
            best = min(venues.values(), key=lambda v: (v.amount, v.venue)) if venues else None
            lowest = floor_quote(best.venue) if best else None
            if lowest is not None:
                lowest.note = "cheapest floor across enabled venues"
        else:
            chain = [primary] + [k for k in ("steam", "skinport") if k != primary]
            others = sorted((v for k, v in venues.items() if k not in chain), key=lambda v: v.amount)
            chain += [v.venue for v in others]
            lowest = next((floor_quote(k) for k in chain if k in venues), None)
            if lowest is not None and lowest.venue != primary:
                lowest.fallback = True
                lowest.note = f"{venue_label(primary)} floor unavailable"

        # Averages ----------------------------------------------------------------------------
        sp_avg = None
        if sp_win.get("avg"):
            amount, metric = sp_win["avg"], "avg"
            if trimmed:
                t = trimmed_from_summary(
                    sp_win["avg"], int(sp_win.get("volume") or 0), sp_win.get("min"), sp_win.get("max")
                )
                if t is not None:
                    amount, metric = t, "trimmed avg"
            sp_avg = Quote(
                amount,
                cur,
                "Skinport",
                metric,
                window,
                volume=sp_win.get("volume"),
                median=sp_win.get("median"),
                max=sp_win.get("max"),
                raw_mean=sp_win["avg"],
                venue="skinport",
            )
            if trimmed and metric == "avg":
                sp_avg.note = "too few sales to trim"
        st_avg = None
        if hist.get("avg"):
            use_trim = trimmed and hist.get("trimmed")
            st_avg = Quote(
                hist["trimmed"] if use_trim else hist["avg"],
                cur,
                "Steam",
                "trimmed avg" if use_trim else "avg",
                window,
                volume=hist.get("volume"),
                median=hist.get("median"),
                max=hist.get("max"),
                raw_mean=hist["avg"],
                venue="steam",
            )
        st_median = (
            Quote(st["median_24h"], cur, "Steam", "median", "24h", volume=st.get("volume_24h"), venue="steam")
            if st.get("median_24h")
            else None
        )
        cf_avg = None
        if csfloat and fx is not None:
            cf_avg = Quote(
                fx.convert(csfloat["avg"]),
                cur,
                "CSFloat",
                "avg",
                window,
                volume=csfloat.get("volume"),
                median=fx.convert(csfloat.get("median")),
                max=fx.convert(csfloat.get("max")),
                raw_mean=fx.convert(csfloat["avg"]),
                fx=fx_label,
                venue="csfloat",
                note="daily averages; float and sticker premiums included",
            )

        if src == "skinport":
            avg_chain = [sp_avg, st_avg, cf_avg, st_median]
        elif src == "steam":
            avg_chain = [st_avg, st_median, sp_avg, cf_avg]
        else:
            avg_chain = [sp_avg, st_avg, cf_avg, st_median]
        avg = next((q for q in avg_chain if q is not None), None)
        if avg is not None and avg is not avg_chain[0]:
            avg.fallback = True
            avg.note = avg.note or f"no {window} sales average from the primary source"

        # Daily sales volume, for the liquidity flag and days-to-sell.
        daily, daily_src = None, None
        if st.get("volume_24h") is not None:
            daily, daily_src = float(st["volume_24h"]), "Steam 24h volume"
        elif sp_win.get("volume") is not None:
            daily, daily_src = (
                sp_win["volume"] / WINDOW_DAYS[window],
                f"Skinport {window} volume / {WINDOW_DAYS[window]}",
            )

        p = PricedItem(
            it,
            lowest=lowest,
            avg=avg,
            daily_volume=daily,
            daily_volume_source=daily_src,
            steam_floor=st.get("lowest") or None,
            skinport_floor=sp_min or None,
            venues=venues,
            alt_avgs={"csfloat": cf_avg} if cf_avg is not None else {},
        )
        apply_flags(p)
        return p


def apply_flags(p: PricedItem) -> None:
    it = p.item
    if not p.priced:
        p.flags.add("unpriced")
    if p.daily_volume is not None and it.quantity > p.daily_volume:
        p.flags.add("illiquid")
    if p.spread is not None and p.spread > 0.5:
        p.flags.add("spread")
    if p.avg is not None and p.avg.skewed:
        p.flags.add("skewed")
    if p.lowest is not None and p.lowest.fallback:
        p.flags.add("fallback_lowest")
    if p.avg is not None and p.avg.fallback:
        p.flags.add("fallback_avg")
    if it.locked_qty:
        p.flags.add("locked")
