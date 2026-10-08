from conftest import make_item
from test_pricing import FakeBackend

from stashcard.demo import DemoBackend
from stashcard.games import GAMES, parse_games
from stashcard.models import Profile
from stashcard.money import seller_receives
from stashcard.pricing import PriceEngine, ScanOptions
from stashcard.report import build_report, source_summary
from stashcard.scan import run_scan


def demo_report(**kw):
    opts = ScanOptions(games=parse_games(["cs2", "dota2", "tf2", "steam"]), **kw)
    return run_scan(None, opts, DemoBackend("demo"))


def test_totals_add_up_from_items():
    r = demo_report()
    assert r.totals.lowest == sum(p.total_lowest or 0 for p in r.items)
    assert r.totals.avg == sum(p.total_avg or 0 for p in r.items)
    assert r.totals.net == sum(p.total_net or 0 for p in r.items)
    assert r.totals.net < r.totals.lowest
    assert r.totals.net_cash == sum(p.total_cash(0.08) or 0 for p in r.items)
    assert sum(g.lowest for g in r.games) == r.totals.lowest
    assert [p.sort_value for p in r.items] == sorted((p.sort_value for p in r.items), reverse=True)


def test_demo_warnings_cover_every_honesty_check():
    r = demo_report()
    codes = {w.code for w in r.warnings}
    assert {"unpriced", "nonmarketable", "liquidity", "skewed", "spread", "locked", "fallback"} <= codes
    assert r.totals.unpriced == 1 and r.totals.skewed >= 1 and r.totals.nonmarketable == 3
    assert r.totals.locked_value > 0 and r.unlock_days == 7
    assert 0 < r.sellable_share < 1 and r.liquidity_grade in "ABCDF"
    assert "Skinport avg" in r.avg_label and "Steam median" in r.avg_label


def test_min_price_filter_counts_hidden_items():
    r = demo_report(min_price=100)
    assert all(p.unit_value >= 100 or not p.priced for p in r.items)
    assert r.totals.below_min > 0 and r.totals.below_min_value > 0


def test_tradable_only_drops_locked():
    r = demo_report(tradable_only=True)
    assert r.totals.locked_qty == 0
    assert all("locked" not in p.flags for p in r.items)


def test_subset_for_one_game():
    r = demo_report()
    sub = r.subset(GAMES["dota2"])
    assert all(p.item.game.key == "dota2" for p in sub.items)
    assert sub.totals.lowest == r.game_totals(GAMES["dota2"]).lowest
    assert any(w.code == "unpriced" for w in sub.warnings)


def test_net_uses_valve_rounding_per_unit():
    eng = PriceEngine(
        FakeBackend({"Case": {"lowest": 19, "volume_24h": 5000}}, {}, {}), ScanOptions(games=[GAMES["cs2"]])
    )
    priced = eng.price([make_item("Case", qty=41)])
    r = build_report(Profile("1", "x"), eng.opts, priced, [], [], [])
    assert r.totals.net == seller_receives(19) * 41 == 17 * 41


def test_source_summary_counts_fallbacks():
    r = demo_report()
    assert source_summary(r.items, "lowest") == "Steam floor · now"
    assert "+7 Steam median · 24h" in source_summary(r.items, "avg")


def test_currency_conversion_in_demo():
    usd = demo_report()
    eur = demo_report(currency="EUR")
    assert 0.85 < eur.totals.lowest / usd.totals.lowest < 0.99
