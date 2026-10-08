"""Marketplaces ("venues"): who quotes what, their seller fees, and the catalog parsers.

Steam money is wallet funds (cannot be withdrawn); every other venue pays cash. Third-party
catalogs are fetched once per game, cached for an hour and looked up locally. They quote USD,
so their numbers are converted with a dated FX rate that is printed wherever they appear.

Fee defaults come from public fee pages and review sites as of 2026-10-29. They are
configurable (--fee venue=rate) because venues change them and some tier them by volume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from stashcard.money import to_minor


@dataclass(frozen=True)
class Venue:
    key: str
    name: str
    apps: tuple[int, ...] | None  # None = every Steam app
    fee: float | None  # default seller fee; None for Steam (Valve's own rounding)
    wallet: bool = False  # Steam wallet funds instead of cash
    experimental: bool = False
    kind: str = "floor"  # "floor" (lowest listing) or "avg" (sales history)
    note: str = ""

    def supports(self, appid: int) -> bool:
        return self.apps is None or appid in self.apps

    @property
    def money_kind(self) -> str:
        return "wallet" if self.wallet else "cash"


VENUES: dict[str, Venue] = {
    "steam": Venue(
        "steam", "Steam Market", None, None, wallet=True, note="priceoverview, per item; 5% Steam + 10% game fee"
    ),
    # TODO(verify): Skinport's standard seller fee (8 %) from review sites; check skinport.com.
    "skinport": Venue("skinport", "Skinport", (730, 570, 440, 252490), 0.08, note="whole-app catalog + sales history"),
    # TODO(verify): Waxpeer default seller fee 6 % (lower tiers exist for volume sellers).
    "waxpeer": Venue("waxpeer", "Waxpeer", (730, 570, 440, 252490), 0.06, note="min in 1/1000 USD; tiny TF2 catalog"),
    # TODO(verify): Market.CSGO sale commission 5 % (withdrawal costs extra and vary).
    "csgotm": Venue(
        "csgotm",
        "Market.CSGO",
        (730, 570, 440, 252490),
        0.05,
        note="market.csgo.com, market.dota2.net, tf2.tm, rust.tm; volume meaning undocumented",
    ),
    # TODO(verify): Lis-skins listing fee 5 % (reviews say 5-6 %; ~10 % applies only to instant sales to bots).
    "lisskins": Venue("lisskins", "Lis-skins", (730, 570, 252490), 0.05, note="CS2, Dota 2, Rust (no TF2 export)"),
    # TODO(verify): CSFloat seller fee 2 %; undocumented history endpoint, experimental and opt-in.
    "csfloat": Venue(
        "csfloat",
        "CSFloat",
        (730,),
        0.02,
        experimental=True,
        kind="avg",
        note="daily sales averages incl. float/sticker premiums; top items only",
    ),
}

DEFAULT_MARKETS = ("steam", "skinport", "waxpeer", "csgotm", "lisskins")
FLOOR_FEEDS = ("waxpeer", "csgotm", "lisskins")  # whole-catalog USD floor feeds

WAXPEER_GAME = {730: "csgo", 570: "dota2", 440: "tf2", 252490: "rust"}
CSGOTM_HOST = {730: "market.csgo.com", 570: "market.dota2.net", 440: "tf2.tm", 252490: "rust.tm"}
LISSKINS_GAME = {730: "csgo", 570: "dota2", 252490: "rust"}


def catalog_url(venue: str, appid: int) -> str | None:
    if venue == "waxpeer" and appid in WAXPEER_GAME:
        return f"https://api.waxpeer.com/v1/prices?game={WAXPEER_GAME[appid]}&minified=1"
    if venue == "csgotm" and appid in CSGOTM_HOST:
        return f"https://{CSGOTM_HOST[appid]}/api/v2/prices/USD.json"
    if venue == "lisskins" and appid in LISSKINS_GAME:
        # Never the 140 MB api_csgo_full.json: the market export is the compact floor list.
        return f"https://lis-skins.com/market_export_json/{LISSKINS_GAME[appid]}.json"
    return None


def csfloat_url(name: str) -> str:
    from urllib.parse import quote

    return f"https://csfloat.com/api/v1/history/{quote(name, safe='')}/graph"


def parse_markets(text: str | list[str] | None) -> list[str]:
    if not text:
        return list(DEFAULT_MARKETS)
    parts = text if isinstance(text, list) else [text]
    out: list[str] = []
    for chunk in parts:
        for raw in str(chunk).split(","):
            key = raw.strip().lower().replace("-", "").replace(".", "").replace("_", "")
            key = {"marketcsgo": "csgotm", "tm": "csgotm", "csgomarket": "csgotm", "lis": "lisskins"}.get(key, key)
            if not key:
                continue
            if key == "all":
                for k in VENUES:
                    if k not in out:
                        out.append(k)
                continue
            if key not in VENUES:
                raise ValueError(f"Unknown marketplace {raw!r}. Use: {', '.join(VENUES)}")
            if key not in out:
                out.append(key)
    return out


def parse_fee_overrides(items: list[str] | None) -> dict[str, float]:
    """['waxpeer=0.05', 'skinport=6%'] -> {'waxpeer': 0.05, 'skinport': 0.06}."""
    out: dict[str, float] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Fee must look like venue=rate (for example waxpeer=0.05), got {item!r}")
        venue, value = item.split("=", 1)
        venue = parse_markets(venue)[0]
        value = value.strip()
        rate = float(value[:-1]) / 100 if value.endswith("%") else float(value)
        if not 0 <= rate < 1:
            raise ValueError(f"Fee for {venue} must be between 0 and 1")
        if venue == "steam":
            raise ValueError("Steam fees follow Valve's own rounding and cannot be overridden")
        out[venue] = rate
    return out


# Parsers ------------------------------------------------------------------------------------
def _keep_min(out: dict, name: str, usd: int | None, count: int) -> None:
    if not name or usd is None or usd <= 0:
        return
    cur = out.get(name)
    if cur is None or usd < cur["usd"]:
        out[name] = {"usd": usd, "count": count if cur is None else max(count, cur["count"])}


def parse_waxpeer(doc: dict) -> dict[str, dict]:
    """Waxpeer /v1/prices?minified=1: {"items": [{"name", "min" (1/1000 USD), "count"}]}."""
    out: dict[str, dict] = {}
    for row in (doc or {}).get("items") or []:
        try:
            usd = int(round(int(row["min"]) / 10))
        except (KeyError, TypeError, ValueError):
            continue
        _keep_min(out, row.get("name"), usd, int(row.get("count") or 0))
    return out


def parse_csgotm(doc: dict) -> dict[str, dict]:
    """Market.CSGO /api/v2/prices/USD.json: {"items": [{"market_hash_name", "price": "10.827", "volume"}]}."""
    out: dict[str, dict] = {}
    for row in (doc or {}).get("items") or []:
        try:
            usd = to_minor(row["price"])
            count = int(str(row.get("volume") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        _keep_min(out, row.get("market_hash_name"), usd, count)
    return out


def parse_lisskins(doc: list) -> dict[str, dict]:
    """Lis-skins market export: [{"name", "price", "unlocked_price", "count"}] in USD."""
    out: dict[str, dict] = {}
    if not isinstance(doc, list):
        return out
    for row in doc:
        try:
            usd = to_minor(row["price"])
        except (KeyError, TypeError):
            continue
        _keep_min(out, row.get("name"), usd, int(row.get("count") or 0))
    return out


PARSERS = {"waxpeer": parse_waxpeer, "csgotm": parse_csgotm, "lisskins": parse_lisskins}


def csfloat_stats(rows: list, window_days: int, now: datetime | None = None) -> dict | None:
    """CSFloat /history/<name>/graph rows (daily avg in cents + count) -> window stats in USD minor.

    rows: [{"day": "2026-10-29T00:00:00Z", "avg_price": 4753.5, "count": 31}] or compact
    [["2026-10-29", 4754, 31]]. Daily averages only, so 'max' is the highest daily average and
    'median' is the volume-weighted median of daily averages.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    days: list[tuple[float, int]] = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                day, avg, count = row["day"], float(row["avg_price"]), int(row["count"])
            else:
                day, avg, count = row[0], float(row[1]), int(row[2])
            stamp = datetime.strptime(str(day)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if stamp >= since.replace(hour=0, minute=0, second=0, microsecond=0) and count > 0:
            days.append((avg, count))
    vol = sum(c for _, c in days)
    if not vol:
        return None
    mean = sum(a * c for a, c in days) / vol
    ordered = sorted(days)
    seen, median = 0, ordered[-1][0]
    for a, c in ordered:
        seen += c
        if seen >= vol / 2:
            median = a
            break
    return {"avg": int(round(mean)), "median": int(round(median)), "max": int(round(ordered[-1][0])), "volume": vol}
