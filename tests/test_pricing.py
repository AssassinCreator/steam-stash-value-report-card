import math

import pytest
from conftest import make_item

from stashcard.games import GAMES
from stashcard.net import RateLimited
from stashcard.pricing import PriceEngine, ScanOptions
from stashcard.sources import combine_skinport_history, combine_skinport_items, history_stats, trimmed_from_summary

REDLINE = "AK-47 | Redline (Field-Tested)"


class FakeBackend:
    """Minimal backend: dicts in, quotes out. Set steam_error to simulate a 429."""

    is_demo = False

    def __init__(
        self, steam=None, sp_items=None, sp_hist=None, steam_error=None, history=None, venues=None, csfloat=None
    ):
        self.venues = venues
        self.csfloat = csfloat
        self.steam = steam or {}
        self.sp_items = sp_items or {}
        self.sp_hist = sp_hist or {}
        self.steam_error = steam_error
        self.history = history
        self.steam_calls = 0

    def steam_price(self, game, name, currency):
        self.steam_calls += 1
        if self.steam_error and self.steam_calls >= self.steam_error:
            raise RateLimited("Steam", 60)
        return self.steam.get(name), None

    def steam_history(self, game, name, currency):
        return self.history

    def skinport_items(self, game, currency):
        return self.sp_items, None

    def skinport_history(self, game, currency):
        return self.sp_hist, None

    def venue_catalog(self, venue, game):
        return (self.venues or {}).get(venue), None

    def fx_table(self):
        from stashcard.fx import FxTable

        return FxTable("USD", {"EUR": 0.9, "RUB": 80.0}, "2026-10-29", "test"), None

    def csfloat_history(self, name):
        return (self.csfloat or {}).get(name)


def opts(**kw):
    return ScanOptions(games=[GAMES["cs2"]], **kw)


SP_HIST = {
    REDLINE: {
        "7d": {"avg": 3220, "median": 2562, "max": 19500, "min": 1980, "volume": 60},
        "30d": {"avg": 3985, "median": 2665, "max": 211400, "min": 1684, "volume": 309},
    }
}
STEAM = {REDLINE: {"lowest": 3190, "median_24h": 3200, "volume_24h": 67}}


def test_default_both_labels_sources():
    p = PriceEngine(FakeBackend(STEAM, {REDLINE: {"min": 2950}}, SP_HIST), opts()).price([make_item()])[0]
    assert p.lowest.label == "Steam floor · now" and p.lowest.amount == 3190 and not p.lowest.fallback
    assert p.avg.label == "Skinport avg · 7d" and p.avg.amount == 3220 and not p.avg.fallback
    assert p.avg.median == 2562
    assert p.steam_floor == 3190 and p.skinport_floor == 2950
    assert p.unit_net == 2775


def test_redline_30d_is_skewed_by_one_sale():
    # The coordinator's real example: 30d mean $39.85 vs median $26.65 because of one $2,114 sale.
    p = PriceEngine(FakeBackend(STEAM, {}, SP_HIST), opts(window="30d")).price([make_item()])[0]
    assert p.avg.amount == 3985 and p.avg.skewed and "skewed" in p.flags


def test_trimmed_avg_is_opt_in_and_labelled():
    p = PriceEngine(FakeBackend(STEAM, {}, SP_HIST), opts(window="30d", trimmed_avg=True)).price([make_item()])[0]
    assert p.avg.metric == "trimmed avg" and "trimmed" in p.avg.label
    assert p.avg.amount == trimmed_from_summary(3985, 309, 1684, 211400)
    assert p.avg.amount < 3985
    assert p.avg.raw_mean == 3985 and p.avg.skewed  # skew is judged on the untrimmed mean


def test_fallback_to_steam_median_is_labelled():
    p = PriceEngine(FakeBackend(STEAM, {}, {}), opts()).price([make_item()])[0]
    assert p.avg.label == "Steam median · 24h" and p.avg.fallback
    assert "fallback_avg" in p.flags


def test_steam_floor_missing_falls_back_to_skinport():
    p = PriceEngine(FakeBackend({}, {REDLINE: {"min": 2950}}, SP_HIST), opts()).price([make_item()])[0]
    assert p.lowest.source == "Skinport" and p.lowest.fallback and "Steam floor unavailable" in p.lowest.note


def test_skinport_mode_prefers_skinport():
    p = PriceEngine(FakeBackend(STEAM, {REDLINE: {"min": 2950}}, SP_HIST), opts(source="skinport")).price(
        [make_item()]
    )[0]
    assert p.lowest.source == "Skinport" and not p.lowest.fallback


def test_steam_mode_without_cookie_uses_median_and_says_so():
    p = PriceEngine(FakeBackend(STEAM, {}, SP_HIST), opts(source="steam")).price([make_item()])[0]
    assert p.avg.source == "Steam" and p.avg.metric == "median" and p.avg.fallback


def _rows(*pairs):
    from datetime import datetime, timedelta, timezone

    day = datetime.now(timezone.utc) - timedelta(days=1)
    return [
        [(day + timedelta(hours=i)).strftime("%b %d %Y %H: +0"), price, str(vol)]
        for i, (price, vol) in enumerate(pairs)
    ]


def test_history_stats_and_trimmed():
    stats = history_stats(_rows((31.0, 10), (33.0, 10)), "7d")
    assert stats["avg"] == 3200 and stats["volume"] == 20 and stats["max"] == 3300
    spiky = history_stats(_rows(*[(30.0, 1)] * 18, (500.0, 1), (1.0, 1)), "7d")
    assert spiky["trimmed"] == 3000  # middle 80 % of volume drops the extremes
    assert spiky["avg"] > 5000


def test_steam_mode_with_history_cookie():
    rows = _rows((31.0, 10), (33.0, 10))
    p = PriceEngine(FakeBackend(STEAM, {}, {}, history=rows), opts(source="steam")).price([make_item()])[0]
    assert p.avg.label == "Steam avg · 7d" and p.avg.amount == 3200 and not p.avg.fallback
    assert p.avg.volume == 20


def test_unpriced_never_zero():
    p = PriceEngine(FakeBackend({}, {}, {}), opts()).price([make_item("Mystery Item")])[0]
    assert p.lowest is None and p.avg is None and "unpriced" in p.flags
    assert p.total_lowest is None


def test_liquidity_spread_and_days_to_sell():
    steam = {
        "Dragonclaw Hook": {"lowest": 37499, "median_24h": None, "volume_24h": 0},
        "Revolution Case": {"lowest": 19, "median_24h": 19, "volume_24h": 9800},
        "Sticker | Crown (Foil)": {"lowest": 40602, "median_24h": 39000, "volume_24h": 1},
    }
    hist = {"Sticker | Crown (Foil)": {"7d": {"avg": 24767, "median": 24360, "max": 30000, "min": 20000, "volume": 6}}}
    items = [
        make_item("Dragonclaw Hook", "dota2"),
        make_item("Revolution Case", qty=41),
        make_item("Sticker | Crown (Foil)"),
    ]
    ps = {p.item.market_hash_name: p for p in PriceEngine(FakeBackend(steam, {}, hist), opts(capture=0.1)).price(items)}
    assert "illiquid" in ps["Dragonclaw Hook"].flags
    assert ps["Dragonclaw Hook"].days_to_sell(0.1) == math.inf
    assert "illiquid" not in ps["Revolution Case"].flags
    assert ps["Revolution Case"].days_to_sell(0.1) == pytest.approx(41 / 980)
    assert "spread" in ps["Sticker | Crown (Foil)"].flags
    assert ps["Sticker | Crown (Foil)"].spread == pytest.approx((40602 - 24767) / 24767)


def test_rate_limit_falls_back_and_reports():
    items = [make_item("A", qty=1), make_item("B", qty=1)]
    sp = {"A": {"min": 500}, "B": {"min": 100}}
    eng = PriceEngine(FakeBackend({"A": {"lowest": 600, "volume_24h": 3}}, sp, {}, steam_error=2), opts())
    ps = {p.item.market_hash_name: p for p in eng.price(items)}
    assert ps["A"].lowest.source == "Steam"  # most valuable asked first
    assert ps["B"].lowest.source == "Skinport" and ps["B"].lowest.fallback
    assert eng.stats.steam_rate_limited
    assert any(n.kind == "ratelimit" for n in eng.notices)


def test_steam_budget_caps_live_lookups():
    items = [make_item(f"Item {i}") for i in range(5)]
    sp = {f"Item {i}": {"min": 100 * (i + 1)} for i in range(5)}
    eng = PriceEngine(FakeBackend({}, sp, {}), opts(steam_budget=2))
    eng.price(items)
    assert eng.stats.steam_live == 2 and eng.stats.steam_skipped == 3
    assert any("capped" in n.text for n in eng.notices)


def test_currency_without_skinport_is_noticed():
    eng = PriceEngine(FakeBackend(STEAM, {}, SP_HIST), ScanOptions(games=[GAMES["cs2"]], currency="UAH"))
    p = eng.price([make_item()])[0]
    assert p.avg.label.startswith("Steam median")
    assert any("Skinport does not quote UAH" in n.text for n in eng.notices)


def test_skinport_rows_combine_versions():
    items = combine_skinport_items(
        [
            {"market_hash_name": "K", "min_price": 10.5, "quantity": 2},
            {"market_hash_name": "K", "min_price": 9.25, "quantity": 3},
            {"market_hash_name": "L", "min_price": None, "quantity": 0},
        ]
    )
    assert items["K"] == {"min": 925, "quantity": 5} and items["L"]["min"] is None
    hist = combine_skinport_history(
        [
            {"market_hash_name": "K", "last_7_days": {"avg": 10, "median": 9, "max": 12, "min": 8, "volume": 1}},
            {"market_hash_name": "K", "last_7_days": {"avg": 20, "median": 19, "max": 25, "min": 15, "volume": 3}},
        ]
    )
    assert hist["K"]["7d"] == {"avg": 1750, "median": None, "max": 2500, "min": 800, "volume": 4}


def test_trimmed_needs_enough_sales():
    assert trimmed_from_summary(1000, 4, 900, 1100) is None
    assert trimmed_from_summary(1000, 10, 100, 5000) == round((10000 - 5100) / 8)
