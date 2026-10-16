from conftest import inventory_blob

from stashcard.games import GAMES, parse_game, parse_games
from stashcard.inventory import display_name, join_inventory, short_name
from stashcard.models import parse_lock_date

RARITY = {
    "tags": [
        {"category": "Rarity", "localized_tag_name": "Classified", "color": "d32ce6"},
        {"category": "Exterior", "localized_tag_name": "Field-Tested"},
    ]
}


def test_join_groups_by_name_and_sums_amounts():
    raw = inventory_blob(
        [
            ("1", "0", "AK-47 | Redline (Field-Tested)", 1, RARITY),
            ("1", "0", "AK-47 | Redline (Field-Tested)", 1, RARITY),
            ("2", "0", "Revolution Case", 1, {}),
            ("2", "0", "Revolution Case", 1, {}),
            ("2", "0", "Revolution Case", 1, {}),
            ("3", "0", "Sack of Gems", 7, {"commodity": 1}),
        ]
    )
    items = {i.market_hash_name: i for i in join_inventory(raw, GAMES["cs2"])}
    assert items["AK-47 | Redline (Field-Tested)"].quantity == 2
    assert items["Revolution Case"].quantity == 3
    assert items["Sack of Gems"].quantity == 7
    ak = items["AK-47 | Redline (Field-Tested)"]
    assert ak.rarity == "Classified" and ak.rarity_color == "d32ce6" and ak.exterior == "Field-Tested"
    assert ak.rarity_label == "Classified"
    assert len(ak.asset_ids) == 2


def test_join_matches_on_classid_and_instanceid():
    raw = inventory_blob(
        [
            ("9", "111", "Genuine Kantusa the Script Sword", 1, {}),
            ("9", "222", "Autographed Kantusa the Script Sword", 1, {}),
        ]
    )
    names = sorted(i.market_hash_name for i in join_inventory(raw, GAMES["dota2"]))
    assert names == ["Autographed Kantusa the Script Sword", "Genuine Kantusa the Script Sword"]


def test_assets_without_description_are_skipped():
    raw = inventory_blob([("1", "0", "AK-47 | Redline (Field-Tested)", 1, {})])
    raw["assets"].append({"classid": "404", "instanceid": "0", "amount": "1", "assetid": "77"})
    assert sum(i.quantity for i in join_inventory(raw, GAMES["cs2"])) == 1


def test_trade_hold_and_tradable_only():
    hold = {
        "tradable": 0,
        "marketable": 0,
        "owner_descriptions": [{"type": "html", "value": "Tradable/Marketable After Nov 05, 2026 (7:00:00) GMT"}],
    }
    raw = inventory_blob(
        [
            ("5", "0", "Kilowatt Case", 1, {}),
            ("5", "77", "Kilowatt Case", 1, hold),
            ("6", "0", "Service Medal 2024", 1, {"marketable": 0, "tradable": 0}),
        ]
    )
    items = {i.market_hash_name: i for i in join_inventory(raw, GAMES["cs2"])}
    case = items["Kilowatt Case"]
    assert case.quantity == 2 and case.tradable_qty == 1 and case.locked_qty == 1
    assert case.marketable  # trade-held copies still count as marketable stock
    assert case.unlock_at.isoformat() == "2026-11-05T07:00:00+00:00"
    assert not items["Service Medal 2024"].marketable
    only = {i.market_hash_name: i for i in join_inventory(raw, GAMES["cs2"], tradable_only=True)}
    assert only["Kilowatt Case"].quantity == 1
    assert "Service Medal 2024" not in only


def test_lock_date_formats():
    assert parse_lock_date("Trade Protected until Nov 4, 2026 (7:00:00) GMT").day == 4
    assert parse_lock_date("nothing here") is None


def test_names():
    raw = inventory_blob([("8", "0", "730-IDF (Foil)", 1, {"name": "IDF (Foil)"})])
    item = join_inventory(raw, GAMES["steam"])[0]
    assert display_name(item) == "IDF (Foil)"
    assert short_name("AWP | Asiimov (Field-Tested)") == "AWP | Asiimov (FT)"
    assert short_name("★ Karambit | Doppler (Factory New)") == "★ Karambit | Doppler (FN)"


def test_games():
    assert [g.key for g in parse_games(["cs2,dota", "753"])] == ["cs2", "dota2", "steam"]
    custom = parse_game("252490/2")
    assert custom.key == "rust"
    other = parse_game("1234/5")
    assert other.custom and other.appid == 1234 and other.contextid == 5
    assert parse_games(None)[0].key == "cs2"
