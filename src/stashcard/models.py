"""Plain data structures shared by the fetch, pricing, report and render layers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from stashcard.games import Game
from stashcard.money import Wallet, seller_receives

WINDOWS = ("24h", "7d", "30d", "90d")
WINDOW_DAYS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}

SKEW_MEAN_MEDIAN = 0.25  # mean vs median differ by more than 25 %
SKEW_MAX_MEDIAN = 5.0  # or one sale above 5x the median


@dataclass
class Profile:
    steamid64: str
    persona: str
    vanity: str | None = None
    avatar_url: str | None = None
    member_since: str | None = None
    privacy: str = "unknown"  # public | friendsonly | private | unknown
    online_state: str | None = None
    vac_banned: bool | None = None
    trade_ban: str | None = None
    limited: bool | None = None
    level: int | None = None
    games_owned: int | None = None
    hours_2wk: float | None = None
    location: str | None = None
    is_demo: bool = False

    @property
    def url(self) -> str:
        if self.vanity:
            return f"steamcommunity.com/id/{self.vanity}"
        return f"steamcommunity.com/profiles/{self.steamid64}"

    @property
    def member_years(self) -> int | None:
        since = parse_member_since(self.member_since)
        if since is None:
            return None
        now = datetime.now()
        years = now.year - since.year - ((now.month, now.day) < (since.month, since.day))
        return max(years, 0)


def parse_member_since(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%B %Y", "%d %B, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


_LOCK_DATE = re.compile(r"([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})(?:\s*\((\d{1,2}):(\d{2}):(\d{2})\))?")


def parse_lock_date(text: str | None) -> datetime | None:
    """'Nov 05, 2026 (7:00:00) GMT' -> aware datetime in UTC."""
    if not text:
        return None
    m = _LOCK_DATE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", "")
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            day = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    h, mi, s = (int(m.group(i) or 0) for i in (2, 3, 4))
    return day.replace(hour=h, minute=mi, second=s, tzinfo=timezone.utc)


@dataclass
class Item:
    """One stack: every copy of the same market_hash_name in one game's inventory."""

    game: Game
    market_hash_name: str
    name: str
    type: str = ""
    icon_url: str = ""
    name_color: str | None = None
    rarity: str | None = None
    rarity_color: str | None = None
    exterior: str | None = None
    quality: str | None = None
    quantity: int = 0
    tradable_qty: int = 0
    marketable: bool = True
    commodity: bool = False
    trade_hold: str | None = None  # Steam's text, e.g. "Nov 05, 2026 (7:00:00) GMT"
    asset_ids: list[str] = field(default_factory=list)

    @property
    def accent(self) -> str | None:
        """Colour used for rarity strips: rarity tag colour, else the item's name colour."""
        c = self.rarity_color or self.name_color
        return c if c and len(c) == 6 else None

    @property
    def rarity_label(self) -> str | None:
        """Text twin of the rarity colour, so colour is never the only signal."""
        if self.rarity:
            return self.rarity
        if self.quality and self.quality not in ("Normal", "Standard", "Unique"):
            return self.quality
        return None

    @property
    def key(self) -> str:
        return f"{self.game.appid}|{self.market_hash_name}"

    @property
    def locked_qty(self) -> int:
        return max(0, self.quantity - self.tradable_qty) if self.marketable else 0

    @property
    def unlock_at(self) -> datetime | None:
        return parse_lock_date(self.trade_hold)


@dataclass
class Quote:
    """One number with its provenance. Never shown without its label."""

    amount: int  # minor units, per unit
    currency: str
    source: str  # "Steam" | "Skinport"
    metric: str  # "floor" | "avg" | "trimmed avg" | "median"
    window: str  # "now" | "24h" | "7d" | "30d" | "90d"
    volume: int | None = None  # sales in the window (avg/median) or listings (floor)
    fallback: bool = False  # True when this is NOT the metric/source the user asked for
    note: str = ""
    median: int | None = None  # window median, shown next to AVG
    max: int | None = None  # highest single sale in the window
    raw_mean: int | None = None  # untrimmed mean, when ``amount`` is a trimmed average
    fx: str | None = None  # FX used when the venue quotes another currency, e.g. "USD->EUR 0.8972 @2026-10-29"
    venue: str | None = None  # venue key ("steam", "waxpeer", ...); None for legacy snapshots

    @property
    def label(self) -> str:
        return f"{self.source} {self.metric} · {self.window}"

    @property
    def short(self) -> str:
        if self.metric == "floor":
            return f"{self.source} floor"
        return f"{self.source} {self.metric} {self.window}"

    @property
    def skewed(self) -> bool:
        """Mean pulled by outliers: |mean - median| > 25 % of median, or max > 5x median."""
        if not self.median or self.metric not in ("avg", "trimmed avg"):
            return False
        mean = self.raw_mean or self.amount
        if abs(mean - self.median) / self.median > SKEW_MEAN_MEDIAN:
            return True
        return bool(self.max and self.max > SKEW_MAX_MEDIAN * self.median)

    def to_dict(self) -> dict:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "source": self.source,
            "metric": self.metric,
            "window": self.window,
            "volume": self.volume,
            "fallback": self.fallback,
            "note": self.note,
            "median": self.median,
            "max": self.max,
            "raw_mean": self.raw_mean,
            "fx": self.fx,
            "venue": self.venue,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> Quote | None:
        if not d:
            return None
        return cls(
            amount=int(d["amount"]),
            currency=d["currency"],
            source=d["source"],
            metric=d["metric"],
            window=d["window"],
            volume=d.get("volume"),
            fallback=bool(d.get("fallback")),
            note=d.get("note", ""),
            median=d.get("median"),
            max=d.get("max"),
            raw_mean=d.get("raw_mean"),
            fx=d.get("fx"),
            venue=d.get("venue"),
        )


@dataclass
class VenueQuote:
    """A floor at one marketplace, in the report currency, with its fee-adjusted net."""

    venue: str  # key in markets.VENUES
    amount: int  # floor per unit, report currency minor units
    net: int | None  # what the seller receives per unit after the venue's fee
    wallet: bool = False  # Steam wallet funds (True) or cash (False)
    fee: float | None = None  # seller fee used; None = Valve's own rounding
    count: int | None = None  # listings on offer, when the venue says
    native: int | None = None  # original USD minor units before FX
    fx: str | None = None  # FX label when converted

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> VenueQuote:
        return cls(**{k: d.get(k) for k in ("venue", "amount", "net", "wallet", "fee", "count", "native", "fx")})


@dataclass
class PricedItem:
    item: Item
    lowest: Quote | None = None
    avg: Quote | None = None
    daily_volume: float | None = None
    daily_volume_source: str | None = None
    steam_floor: int | None = None  # raw floors, for the wallet vs cash net view
    skinport_floor: int | None = None
    flags: set[str] = field(default_factory=set)
    venues: dict[str, VenueQuote] = field(default_factory=dict)  # floors at every enabled marketplace
    alt_avgs: dict[str, Quote] = field(default_factory=dict)  # second AVG sources (CSFloat)

    def best_buy(self) -> VenueQuote | None:
        """Cheapest floor anywhere (wallet or cash)."""
        quotes = list(self.venues.values())
        return min(quotes, key=lambda v: (v.amount, v.venue)) if quotes else None

    def best_sell_cash(self) -> VenueQuote | None:
        """Highest net after fees among cash venues (Steam wallet funds excluded on purpose)."""
        quotes = [v for v in self.venues.values() if not v.wallet and v.net is not None]
        return max(quotes, key=lambda v: (v.net, v.venue)) if quotes else None

    @property
    def qty(self) -> int:
        return self.item.quantity

    @property
    def priced(self) -> bool:
        return self.lowest is not None or self.avg is not None

    @property
    def unit_net(self) -> int | None:
        """Seller receives on Steam (wallet funds) at the Steam floor, after Valve's fees."""
        if self.steam_floor is None:
            return None
        return seller_receives(self.steam_floor, Wallet(publisher_fee=self.item.game.publisher_fee))

    def unit_cash(self, fee: float) -> int | None:
        """Seller receives on Skinport (cash) at the Skinport floor, after its seller fee."""
        if self.skinport_floor is None:
            return None
        return int(math.floor(self.skinport_floor * (1 - fee)))

    @property
    def total_lowest(self) -> int | None:
        return None if self.lowest is None else self.lowest.amount * self.qty

    @property
    def total_avg(self) -> int | None:
        return None if self.avg is None else self.avg.amount * self.qty

    @property
    def total_net(self) -> int | None:
        net = self.unit_net
        return None if net is None else net * self.qty

    def total_cash(self, fee: float) -> int | None:
        cash = self.unit_cash(fee)
        return None if cash is None else cash * self.qty

    @property
    def unit_value(self) -> int:
        """Best single-unit figure for sorting and the min-price filter."""
        if self.lowest is not None:
            return self.lowest.amount
        if self.avg is not None:
            return self.avg.amount
        return 0

    @property
    def sort_value(self) -> int:
        return self.unit_value * self.qty

    @property
    def locked_value(self) -> int:
        return self.unit_value * self.item.locked_qty

    @property
    def spread(self) -> float | None:
        """Relative disagreement between floor and average: |L - A| / min(L, A)."""
        if self.lowest is None or self.avg is None:
            return None
        lo, av = self.lowest.amount, self.avg.amount
        if min(lo, av) <= 0:
            return None
        return abs(lo - av) / min(lo, av)

    def days_to_sell(self, capture: float) -> float | None:
        """days ~ quantity / (daily sales x capture). None = unknown, inf = no recent sales."""
        if self.daily_volume is None:
            return None
        rate = self.daily_volume * capture
        if rate <= 0:
            return math.inf
        return self.qty / rate
