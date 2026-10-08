import httpx
import pytest
from conftest import make_item
from test_pricing import FakeBackend

from stashcard.cache import Cache
from stashcard.demo import DemoBackend
from stashcard.fx import FxRate, FxTable, parse_er_api
from stashcard.games import GAMES, parse_games
from stashcard.markets import (
    DEFAULT_MARKETS,
    VENUES,
    catalog_url,
    csfloat_stats,
    parse_csgotm,
    parse_fee_overrides,
    parse_lisskins,
    parse_markets,
    parse_waxpeer,
)
from stashcard.net import Net
from stashcard.pricing import PriceEngine, ScanOptions
from stashcard.report import build_report
from stashcard.scan import run_scan
from stashcard.snapshot import report_from_dict, report_to_dict
from stashcard.sources import LiveBackend

REDLINE = "AK-47 | Redline (Field-Tested)"


# Parsers: shapes verified live on 2026-10-29 -------------------------------------------------
def test_parse_waxpeer_thousandths_of_usd():
    doc = {
        "success": True,
        "items": [
            {"name": REDLINE, "count": 130, "min": 25510},
            {"name": REDLINE, "count": 3, "min": 26000},
            {"name": "Broken", "min": "x"},
            {"name": "Zero", "min": 0, "count": 1},
        ],
    }
    assert parse_waxpeer(doc) == {REDLINE: {"usd": 2551, "count": 130}}


def test_parse_csgotm_string_prices_keep_minimum():
    doc = {
        "success": True,
        "currency": "USD",
        "items": [
            {"market_hash_name": REDLINE, "volume": "25", "price": "25.326"},
            {"market_hash_name": REDLINE, "volume": "4", "price": "26.000"},
            {"market_hash_name": "Case", "volume": "0", "price": "0.139"},
        ],
    }
    out = parse_csgotm(doc)
    assert out[REDLINE] == {"usd": 2533, "count": 25} and out["Case"]["usd"] == 14


def test_parse_lisskins_list():
    doc = [{"name": REDLINE, "price": 25.17, "unlocked_price": 25.17, "count": 105, "url": "x"}, {"name": "No price"}]
    assert parse_lisskins(doc) == {REDLINE: {"usd": 2517, "count": 105}}
    assert parse_lisskins({"error": True}) == {}


def test_csfloat_stats_window_and_skew():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    rows = [
        {"day": "2026-10-29T00:00:00Z", "avg_price": 4753.5, "count": 31},
        {"day": "2026-10-28T00:00:00Z", "avg_price": 4287.1, "count": 66},
        ["2026-10-27", 10059, 102],
        ["2026-09-01", 99999, 500],
    ]  # outside a 7 day window
    st = csfloat_stats(rows, 7, now)
    assert st["volume"] == 199 and st["max"] == 10059
    assert st["avg"] == round((4753.5 * 31 + 4287.1 * 66 + 10059 * 102) / 199)
    assert csfloat_stats([], 7, now) is None


def test_catalog_urls_and_supported_games():
    assert catalog_url("waxpeer", 730).endswith("game=csgo&minified=1")
    assert catalog_url("csgotm", 440) == "https://tf2.tm/api/v2/prices/USD.json"
    assert catalog_url("lisskins", 440) is None  # verified: no TF2 export (404)
    assert "full" not in catalog_url("lisskins", 730)
    assert not VENUES["lisskins"].supports(440) and VENUES["csfloat"].experimental


def test_parse_markets_and_fees():
    assert parse_markets(None) == list(DEFAULT_MARKETS)
    assert parse_markets("steam, market.csgo,lis-skins") == ["steam", "csgotm", "lisskins"]
    assert "csfloat" in parse_markets("all")
    with pytest.raises(ValueError):
        parse_markets("nope")
    assert parse_fee_overrides(["waxpeer=0.05", "skinport=6%"]) == {"waxpeer": 0.05, "skinport": 0.06}
    with pytest.raises(ValueError):
        parse_fee_overrides(["steam=0.1"])
    with pytest.raises(ValueError):
        parse_fee_overrides(["waxpeer=1.5"])


# FX -------------------------------------------------------------------------------------------
def test_fx_parse_and_convert():
    doc = {
        "result": "success",
        "base_code": "USD",
        "time_last_update_unix": 1793232151,
        "rates": {"USD": 1, "EUR": 0.897215, "RUB": 80.531694},
    }
    table = parse_er_api(doc)
    assert table.date == "2026-10-29" and table.source == "open.er-api.com"
    eur = table.rate("EUR")
    assert eur.convert(2551) == 2289 and "1 USD = 0.897215 EUR" in eur.label
    assert table.rate("RUB").convert(100) == 8053
    assert table.rate("XXX") is None and table.rate("USD").identity
    with pytest.raises(ValueError):
        parse_er_api({"result": "error"})


# Engine: per-venue net, best venues, lowest-from ------------------------------------------------
FEEDS = {
    "waxpeer": {REDLINE: {"usd": 2551, "count": 130}},
    "csgotm": {REDLINE: {"usd": 2533, "count": 25}},
    "lisskins": {REDLINE: {"usd": 2517, "count": 105}},
}
STEAM = {REDLINE: {"lowest": 3190, "median_24h": 3200, "volume_24h": 67}}


def _price(**kw):
    backend = FakeBackend(STEAM, {REDLINE: {"min": 2957, "quantity": 9}}, {}, venues={k: v for k, v in FEEDS.items()})
    return PriceEngine(backend, ScanOptions(games=[GAMES["cs2"]], **kw)).price([make_item(qty=2)])[0]


def test_per_venue_net_and_wallet_vs_cash():
    p = _price()
    v = p.venues
    assert v["steam"].wallet and v["steam"].net == 2775 and v["steam"].fee is None
    assert not v["waxpeer"].wallet and v["waxpeer"].net == int(2551 * (1 - 0.06))
    assert v["csgotm"].net == int(2533 * 0.95) and v["lisskins"].net == int(2517 * 0.95)
    assert v["skinport"].count == 9 and v["waxpeer"].fx is None  # USD report: no conversion label
    assert p.best_buy().venue == "lisskins"
    assert p.best_sell_cash().venue == "skinport"  # 2957 * 0.92 = 2720 beats the others
    assert p.lowest.source == "Steam" and not p.lowest.fallback


def test_fee_override_changes_best_sell():
    p = _price(fees={"waxpeer": 0.0})
    assert p.venues["waxpeer"].net == 2551 and p.best_sell_cash().venue == "skinport"
    p = _price(fees={"waxpeer": 0.0, "skinport": 0.2})
    assert p.best_sell_cash().venue == "waxpeer"


def test_lowest_from_best_and_named_venue():
    best = _price(lowest_from="best")
    assert best.lowest.source == "Lis-skins" and best.lowest.amount == 2517 and not best.lowest.fallback
    named = _price(lowest_from="waxpeer")
    assert named.lowest.label == "Waxpeer floor · now" and not named.lowest.fallback
    missing = PriceEngine(
        FakeBackend(STEAM, {}, {}, venues={}), ScanOptions(games=[GAMES["cs2"]], lowest_from="waxpeer")
    ).price([make_item()])[0]
    assert missing.lowest.source == "Steam" and missing.lowest.fallback
    assert "Waxpeer floor unavailable" in missing.lowest.note


def test_converted_venues_carry_fx_label():
    backend = FakeBackend(STEAM, {}, {}, venues=FEEDS)
    eng = PriceEngine(backend, ScanOptions(games=[GAMES["cs2"]], currency="EUR"))
    p = eng.price([make_item()])[0]
    assert p.venues["waxpeer"].amount == round(2551 * 0.9) and p.venues["waxpeer"].native == 2551
    assert p.venues["waxpeer"].fx.startswith("USD→EUR 0.9")
    assert p.venues["steam"].fx is None  # Steam quotes EUR natively
    assert eng.fx.rate == 0.9


def test_missing_fx_skips_usd_venues_with_notice():
    class NoFx(FakeBackend):
        def fx_table(self):
            return None, None

    eng = PriceEngine(NoFx(STEAM, {}, {}, venues=FEEDS), ScanOptions(games=[GAMES["cs2"]], currency="EUR"))
    p = eng.price([make_item()])[0]
    assert set(p.venues) == {"steam"}
    assert any("USD only" in n.text for n in eng.notices)


def test_disabled_markets_are_not_fetched():
    class Strict(FakeBackend):
        def venue_catalog(self, venue, game):
            raise AssertionError("should not fetch " + venue)

    p = PriceEngine(Strict(STEAM, {}, {}), ScanOptions(games=[GAMES["cs2"]], markets=["steam"])).price([make_item()])[0]
    assert set(p.venues) == {"steam"}


def test_csfloat_is_second_avg_and_skew_guarded():
    rows = {REDLINE: [["2099-01-01", 4754, 31]]}  # future date: inside any window

    class Cf(FakeBackend):
        as_of = None

    backend = Cf(STEAM, {}, {}, venues={}, csfloat={REDLINE: [[d, a, c] for d, a, c in rows[REDLINE]]})
    eng = PriceEngine(backend, ScanOptions(games=[GAMES["cs2"]], markets=["steam", "csfloat"]))
    p = eng.price([make_item()])[0]
    assert p.alt_avgs["csfloat"].label == "CSFloat avg · 7d"
    assert p.avg.source == "CSFloat" and p.avg.fallback  # beats the Steam 24h median, labelled as fallback


# Report level --------------------------------------------------------------------------------
def test_demo_venue_totals_coverage_and_route():
    r = run_scan(None, ScanOptions(games=parse_games(["cs2", "dota2", "tf2", "steam"])), DemoBackend("demo"))
    vts = {v.venue: v for v in r.venue_totals}
    assert set(vts) == set(DEFAULT_MARKETS)
    assert vts["steam"].wallet and not vts["waxpeer"].wallet
    assert vts["lisskins"].coverage(r.totals.lowest) < vts["waxpeer"].coverage(r.totals.lowest) <= 1
    assert vts["waxpeer"].net < vts["waxpeer"].value
    route, missing = r.best_cash_route
    assert route > 0 and missing > 0  # Steam community items have no cash venue
    assert r.fx is None or r.fx.identity is False


def test_demo_eur_prints_fx_and_roundtrips():
    r = run_scan(None, ScanOptions(games=parse_games(["cs2"]), currency="EUR"), DemoBackend("demo"))
    assert r.fx_used and r.fx.date == "2026-10-29"
    back = report_from_dict(report_to_dict(r))
    assert back.fx.rate == r.fx.rate
    assert {k: v.amount for k, v in back.items[0].venues.items()} == {k: v.amount for k, v in r.items[0].venues.items()}
    assert [v.net for v in back.venue_totals] == [v.net for v in r.venue_totals]


def test_build_report_without_venues_is_unchanged():
    p = PriceEngine(FakeBackend(STEAM, {}, {}), ScanOptions(games=[GAMES["cs2"]], markets=["steam"])).price(
        [make_item()]
    )
    from stashcard.models import Profile

    r = build_report(Profile("1", "x"), ScanOptions(games=[GAMES["cs2"]], markets=["steam"]), p, [], [], [])
    assert [v.venue for v in r.venue_totals] == ["steam"] and not r.fx_used


# Live backend with mocked HTTP ------------------------------------------------------------------
def _backend(handler, tmp_path):
    return LiveBackend(Net(transport=httpx.MockTransport(handler), sleep=lambda s: None), Cache(tmp_path / "c"))


def test_live_venue_catalog_parses_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        assert req.headers["user-agent"].startswith("stashcard/")
        return httpx.Response(200, json={"success": True, "items": [{"name": REDLINE, "min": 25510, "count": 3}]})

    be = _backend(handler, tmp_path)
    data, hit = be.venue_catalog("waxpeer", GAMES["cs2"])
    assert data == {REDLINE: {"usd": 2551, "count": 3}} and hit is None
    data2, hit2 = be.venue_catalog("waxpeer", GAMES["cs2"])
    assert data2 == data and hit2 is not None and len(calls) == 1
    assert be.venue_catalog("lisskins", GAMES["tf2"]) == (None, None)


def test_live_fx_and_csfloat(tmp_path):
    def handler(req):
        if req.url.host == "open.er-api.com":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "base_code": "USD",
                    "time_last_update_unix": 1793232151,
                    "rates": {"EUR": 0.9},
                },
            )
        if req.url.host == "csfloat.com":
            return httpx.Response(
                200,
                json=[{"day": "2026-10-29T00:00:00Z", "avg_price": 4753.5, "count": 31}],
                headers={"x-ratelimit-remaining": "480"},
            )
        return httpx.Response(404)

    be = _backend(handler, tmp_path)
    table, hit = be.fx_table()
    assert isinstance(table, FxTable) and table.rate("EUR").rate == 0.9 and hit is None
    assert be.csfloat_history(REDLINE) == [["2026-10-29", 4753.5, 31]]


def test_fx_rate_roundtrip_dict():
    r = FxRate("USD", "EUR", 0.9, "2026-10-29", "open.er-api.com")
    assert FxRate.from_dict(r.to_dict()) == r
