"""Live data sources: Steam Community (profile, inventory, market), Skinport and the optional Web API.

Every method is cache-first. When a source rate-limits us we fall back to stale cache if we
have it, and the caller is told the age so the report can say "from cache, 2 h old".
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from stashcard.cache import Cache, Hit
from stashcard.fx import FX_URL, FxTable, parse_er_api
from stashcard.games import Game
from stashcard.markets import PARSERS, VENUES, catalog_url, csfloat_url
from stashcard.models import WINDOW_DAYS, Profile
from stashcard.money import CURRENCIES, parse_price, to_minor
from stashcard.net import FetchError, Net, NotFoundError, PrivateError, RateLimited
from stashcard.profile import ProfileRef

COMMUNITY = "https://steamcommunity.com"
ICON_CDN = "https://community.fastly.steamstatic.com/economy/image/{icon}/{size}fx{size}f"
SKINPORT = "https://api.skinport.com/v1"
SKINPORT_WINDOWS = {"24h": "last_24_hours", "7d": "last_7_days", "30d": "last_30_days", "90d": "last_90_days"}


@dataclass
class InventoryResult:
    data: dict
    age: float | None = None  # seconds, when served from cache
    stale: bool = False  # served from expired cache because the live request failed
    note: str = ""


def _xml_text(root: ET.Element, tag: str) -> str | None:
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def parse_profile_xml(text: str) -> Profile:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FetchError("Steam returned an unreadable profile page. Try again in a minute.") from exc
    err = _xml_text(root, "error")
    if err:
        raise NotFoundError(f"Steam says: {err}")
    sid = _xml_text(root, "steamID64")
    if not sid:
        raise NotFoundError("Steam did not return a SteamID64 for that profile.")
    hours = _xml_text(root, "hoursPlayed2Wk")
    try:
        hours_f = float(hours.replace(",", "")) if hours else None
    except ValueError:
        hours_f = None
    return Profile(
        steamid64=sid,
        persona=_xml_text(root, "steamID") or sid,
        vanity=_xml_text(root, "customURL"),
        avatar_url=_xml_text(root, "avatarFull") or _xml_text(root, "avatarMedium"),
        member_since=_xml_text(root, "memberSince"),
        privacy=(_xml_text(root, "privacyState") or "unknown").lower(),
        online_state=_xml_text(root, "onlineState"),
        vac_banned=(_xml_text(root, "vacBanned") == "1") if _xml_text(root, "vacBanned") else None,
        trade_ban=_xml_text(root, "tradeBanState"),
        limited=(_xml_text(root, "isLimitedAccount") == "1") if _xml_text(root, "isLimitedAccount") else None,
        hours_2wk=hours_f,
        location=_xml_text(root, "location"),
    )


def combine_skinport_items(rows: list[dict]) -> dict[str, dict]:
    """Collapse Skinport catalog rows by market_hash_name (Doppler phases come as versions)."""
    out: dict[str, dict] = {}
    for row in rows or []:
        name = row.get("market_hash_name")
        if not name:
            continue
        lo = to_minor(row.get("min_price"))
        qty = int(row.get("quantity") or 0)
        cur = out.setdefault(name, {"min": None, "quantity": 0})
        if lo is not None and (cur["min"] is None or lo < cur["min"]):
            cur["min"] = lo
        cur["quantity"] += qty
    return out


def combine_skinport_history(rows: list[dict]) -> dict[str, dict]:
    """Collapse sales history by name. Versions (Doppler phases) are merged: averages are
    volume-weighted, min/max are the extremes, the median is kept only for a single version."""
    acc: dict[str, dict[str, list[dict]]] = {}
    for row in rows or []:
        name = row.get("market_hash_name")
        if not name:
            continue
        for win, field in SKINPORT_WINDOWS.items():
            block = row.get(field) or {}
            vol = int(block.get("volume") or 0)
            if vol and block.get("avg") is not None:
                acc.setdefault(name, {}).setdefault(win, []).append({**block, "volume": vol})
    out: dict[str, dict] = {}
    for name, wins in acc.items():
        out[name] = {}
        for win, parts in wins.items():
            vol = sum(p["volume"] for p in parts)
            avg = sum(float(p["avg"]) * p["volume"] for p in parts) / vol
            maxes = [float(p["max"]) for p in parts if p.get("max") is not None]
            mins = [float(p["min"]) for p in parts if p.get("min") is not None]
            out[name][win] = {
                "avg": to_minor(avg),
                "median": to_minor(parts[0].get("median")) if len(parts) == 1 else None,
                "max": to_minor(max(maxes)) if maxes else None,
                "min": to_minor(min(mins)) if mins else None,
                "volume": vol,
            }
    return out


def trimmed_from_summary(avg: int, vol: int, lo: int | None, hi: int | None) -> int | None:
    """Mean without the single highest and lowest sale, from Skinport's window summary.

    sum = avg x volume, so (sum - max - min) / (volume - 2). Needs at least 5 sales.
    """
    if vol < 5 or lo is None or hi is None:
        return None
    return max(1, round((avg * vol - hi - lo) / (vol - 2)))


def history_stats(prices: list, window: str, now: datetime | None = None) -> dict | None:
    """Volume-weighted stats of Steam pricehistory rows inside ``window``.

    Rows look like ["Oct 28 2026 01: +0", 31.9, "12"] (each row is a time bucket's median).
    Returns avg, median, max, trimmed (middle 80 % of volume) and volume, in minor units.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS[window])
    rows: list[tuple[float, int]] = []
    for row in prices or []:
        try:
            stamp = datetime.strptime(row[0][:14], "%b %d %Y %H").replace(tzinfo=timezone.utc)
            price, count = float(row[1]), int(str(row[2]).replace(",", ""))
        except (ValueError, IndexError, TypeError):
            continue
        if stamp >= since and count > 0:
            rows.append((price, count))
    vol = sum(c for _, c in rows)
    if not vol:
        return None
    avg = sum(p * c for p, c in rows) / vol
    ordered = sorted(rows)
    half, seen, median = vol / 2, 0, ordered[-1][0]
    for p, c in ordered:
        seen += c
        if seen >= half:
            median = p
            break
    # trimmed: keep the middle 80 % of volume (drop the cheapest and dearest 10 %)
    lo_edge, hi_edge = vol * 0.10, vol * 0.90
    kept_sum, kept_vol, start = 0.0, 0.0, 0
    for p, c in ordered:
        overlap = max(0.0, min(start + c, hi_edge) - max(start, lo_edge))
        kept_sum += p * overlap
        kept_vol += overlap
        start += c
    trimmed = kept_sum / kept_vol if kept_vol else avg
    return {
        "avg": to_minor(avg),
        "median": to_minor(median),
        "max": to_minor(ordered[-1][0]),
        "trimmed": to_minor(trimmed),
        "volume": vol,
    }


class LiveBackend:

    is_demo = False

    def __init__(self, net: Net, cache: Cache, secrets: dict | None = None):
        self.net = net
        self.cache = cache
        self.secrets = secrets or {}

    # Profile ----------------------------------------------------------------------------
    def profile(self, ref: ProfileRef) -> Profile:
        path = f"/profiles/{ref.steamid64}" if ref.steamid64 else f"/id/{quote(ref.vanity or '')}"
        key = path.lower()
        hit = self.cache.get("profile", key)
        if hit is None:
            try:
                resp = self.net.get(f"{COMMUNITY}{path}", params={"xml": 1})
                text = resp.text
                self.cache.put("profile", key, text)
            except (RateLimited, FetchError):
                hit = self.cache.get("profile", key, ttl=None)
                if hit is None:
                    raise
                text = str(hit.data)
        else:
            text = str(hit.data)
        prof = parse_profile_xml(text)
        self._enrich_with_api(prof)
        return prof

    def _enrich_with_api(self, prof: Profile) -> None:
        key = self.secrets.get("api_key")
        if not key:
            return
        # TODO(verify): Web API calls are implemented from Valve's docs but untested here (no key).
        base = "https://api.steampowered.com/IPlayerService"
        try:
            lvl = self.net.get(f"{base}/GetSteamLevel/v1/", params={"key": key, "steamid": prof.steamid64}).json()
            prof.level = lvl.get("response", {}).get("player_level")
            own = self.net.get(
                f"{base}/GetOwnedGames/v1/",
                params={"key": key, "steamid": prof.steamid64, "include_played_free_games": 1},
            ).json()
            prof.games_owned = own.get("response", {}).get("game_count")
        except (FetchError, ValueError):
            pass  # optional extras; never fail the scan for them

    # Inventory --------------------------------------------------------------------------
    def inventory(self, steamid64: str, game: Game) -> InventoryResult:
        key = f"{steamid64}/{game.appid}/{game.contextid}"
        hit = self.cache.get("inventory", key)
        if hit is not None:
            return InventoryResult(hit.data, age=hit.age)
        try:
            data = self._fetch_inventory(steamid64, game)
        except RateLimited:
            stale = self.cache.get("inventory", key, ttl=None)
            if stale is None:
                raise
            return InventoryResult(stale.data, age=stale.age, stale=True, note="Steam rate limit")
        self.cache.put("inventory", key, data)
        return InventoryResult(data)

    def _fetch_inventory(self, steamid64: str, game: Game) -> dict:
        url = f"{COMMUNITY}/inventory/{steamid64}/{game.appid}/{game.contextid}"
        assets: list = []
        descriptions: list = []
        start: str | None = None
        count = 2000
        total = 0
        for _ in range(50):  # 100k items is plenty
            params = {"l": "english", "count": count}
            if start:
                params["start_assetid"] = start
            resp = self.net.get(url, params=params)
            if resp.status_code in (401, 403):
                raise PrivateError(
                    f"The {game.short} inventory is private. Set Steam > Edit Profile > Privacy > "
                    "Inventory to Public, then retry."
                )
            if resp.status_code == 400 and count > 1000:
                count = 1000
                continue
            if resp.status_code == 404:
                raise NotFoundError("Steam does not know that profile.")
            if resp.status_code != 200:
                raise FetchError(f"Steam inventory returned HTTP {resp.status_code}.")
            try:
                page = resp.json()
            except ValueError:
                page = None
            if not page:
                raise PrivateError(f"The {game.short} inventory is private or unavailable.")
            if not page.get("success", 1):
                raise FetchError(page.get("error") or "Steam could not load that inventory.")
            assets += page.get("assets") or []
            descriptions += page.get("descriptions") or []
            total = page.get("total_inventory_count", total)
            if page.get("more_items") and page.get("last_assetid"):
                start = str(page["last_assetid"])
                continue
            break
        return {"assets": assets, "descriptions": descriptions, "total_inventory_count": total, "success": 1}

    # Steam Market -----------------------------------------------------------------------
    def steam_price(self, game: Game, name: str, currency: str) -> tuple[dict | None, Hit | None]:
        """priceoverview: floor ("lowest_price"), 24 h median and 24 h volume."""
        cur = CURRENCIES[currency]
        if not cur.steam_id:
            return None, None
        key = f"{game.appid}|{cur.steam_id}|{name}"
        hit = self.cache.get("steam_price", key)
        if hit is not None:
            return hit.data, hit
        try:
            resp = self.net.get(
                f"{COMMUNITY}/market/priceoverview/",
                params={"appid": game.appid, "currency": cur.steam_id, "market_hash_name": name},
            )
        except RateLimited:
            stale = self.cache.get("steam_price", key, ttl=None)
            if stale is None:
                raise
            return stale.data, stale
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("success"):
            data = None
        else:
            vol = body.get("volume")
            data = {
                "lowest": parse_price(body.get("lowest_price")),
                "median_24h": parse_price(body.get("median_price")),
                "volume_24h": int(str(vol).replace(",", "").replace(".", "")) if vol else 0,
            }
        self.cache.put("steam_price", key, data)
        return data, None

    def steam_history(self, game: Game, name: str, currency: str) -> list | None:
        """Full sales history needs a login this CLI does not use; Skinport is the AVG source."""
        return None

    # Skinport ---------------------------------------------------------------------------
    def _skinport(self, ns: str, endpoint: str, appid: int, currency: str) -> tuple[list | None, Hit | None]:
        key = f"{appid}|{currency}"
        hit = self.cache.get(ns, key)
        if hit is not None:
            return hit.data, hit
        try:
            resp = self.net.get(
                f"{SKINPORT}/{endpoint}",
                params={"app_id": appid, "currency": currency, **({"tradable": 0} if ns == "skinport_items" else {})},
                headers={"Accept-Encoding": "br"},
            )
            if resp.status_code != 200:
                raise FetchError(f"Skinport returned HTTP {resp.status_code}.")
            data = resp.json()
        except (RateLimited, FetchError, ValueError):
            stale = self.cache.get(ns, key, ttl=None)
            if stale is None:
                raise
            return stale.data, stale
        self.cache.put(ns, key, data)
        return data, None

    def skinport_items(self, game: Game, currency: str) -> tuple[dict | None, Hit | None]:
        if not game.skinport or not CURRENCIES[currency].skinport:
            return None, None
        rows, hit = self._skinport("skinport_items", "items", game.appid, currency)
        return combine_skinport_items(rows or []), hit

    def skinport_history(self, game: Game, currency: str) -> tuple[dict | None, Hit | None]:
        if not game.skinport or not CURRENCIES[currency].skinport:
            return None, None
        rows, hit = self._skinport("skinport_history", "sales/history", game.appid, currency)
        return combine_skinport_history(rows or []), hit

    # Third-party venues -----------------------------------------------------------------
    def venue_catalog(self, venue: str, game: Game) -> tuple[dict | None, Hit | None]:
        """Whole-catalog USD floors for one venue and game, parsed to {name: {"usd", "count"}}.

        Fetched once, cached for an hour (the parsed form, which is much smaller than the feed).
        """
        url = catalog_url(venue, game.appid)
        if url is None:
            return None, None
        key = f"{venue}|{game.appid}"
        hit = self.cache.get("venue_catalog", key)
        if hit is not None:
            return hit.data, hit
        try:
            resp = self.net.get(url)
            if resp.status_code != 200:
                raise FetchError(f"{VENUES[venue].name} returned HTTP {resp.status_code}.")
            data = PARSERS[venue](resp.json())
            if not data:
                raise FetchError(f"{VENUES[venue].name} returned an empty catalog.")
        except (RateLimited, FetchError, ValueError):
            stale = self.cache.get("venue_catalog", key, ttl=None)
            if stale is None:
                raise
            return stale.data, stale
        self.cache.put("venue_catalog", key, data)
        return data, None

    def fx_table(self) -> tuple[FxTable | None, Hit | None]:
        hit = self.cache.get("fx", "USD")
        if hit is None:
            try:
                resp = self.net.get(FX_URL)
                if resp.status_code != 200:
                    raise FetchError(f"FX provider returned HTTP {resp.status_code}.")
                doc = resp.json()
                parse_er_api(doc)  # validate before caching
                self.cache.put("fx", "USD", doc)
            except (RateLimited, FetchError, ValueError):
                hit = self.cache.get("fx", "USD", ttl=None)
                if hit is None:
                    raise
                return parse_er_api(hit.data), hit
            return parse_er_api(doc), None
        return parse_er_api(hit.data), hit

    def csfloat_history(self, name: str) -> list | None:
        """Experimental: CSFloat's undocumented daily sales graph (avg_price in USD cents, count).

        TODO(verify): undocumented endpoint; it may change or require login without notice.
        """
        hit = self.cache.get("csfloat", name)
        if hit is not None:
            return hit.data
        resp = self.net.get(csfloat_url(name))
        if resp.status_code == 404:
            self.cache.put("csfloat", name, [])
            return []
        if resp.status_code != 200:
            raise FetchError(f"CSFloat returned HTTP {resp.status_code}.")
        left = resp.headers.get("x-ratelimit-remaining")
        if left is not None and left.isdigit() and int(left) < 20:
            self.net._blocked["csfloat.com"] = time.monotonic() + 300  # stop well before their limit
        try:
            rows = resp.json()
        except ValueError:
            rows = None
        rows = rows if isinstance(rows, list) else []
        compact = [
            [str(r.get("day", ""))[:10], r.get("avg_price"), r.get("count")] for r in rows[:120] if isinstance(r, dict)
        ]
        self.cache.put("csfloat", name, compact)
        return compact

    # Images -----------------------------------------------------------------------------
    def icon(self, icon_url: str, size: int = 256) -> bytes | None:
        if not icon_url:
            return None
        key = f"{icon_url}/{size}"
        data = self.cache.get_bytes("icons", key)
        if data:
            return data
        try:
            resp = self.net.get(ICON_CDN.format(icon=icon_url, size=size))
        except FetchError:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        self.cache.put_bytes("icons", key, resp.content)
        return resp.content

    def avatar(self, url: str | None) -> bytes | None:
        if not url:
            return None
        data = self.cache.get_bytes("avatars", url, ext="img")
        if data:
            return data
        try:
            resp = self.net.get(url)
        except FetchError:
            return None
        if resp.status_code != 200:
            return None
        self.cache.put_bytes("avatars", url, resp.content, ext="img")
        return resp.content

    def now(self) -> float:
        return time.time()
