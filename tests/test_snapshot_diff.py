import json

from stashcard.demo import DemoBackend
from stashcard.diff import compute_diff, delta_from
from stashcard.export import alt_text, write_csv, write_json, write_sidecars
from stashcard.games import parse_games
from stashcard.pricing import ScanOptions
from stashcard.redact import Redaction, at_least, default_level, mask_id, mask_name, value_band
from stashcard.scan import run_scan
from stashcard.snapshot import History, load_snapshot, report_from_dict, report_to_dict, snapshot_hash


def scan(name="demo", epoch="now", **kw):
    return run_scan(
        None, ScanOptions(games=parse_games(["cs2", "dota2", "tf2", "steam"]), **kw), DemoBackend(name, epoch)
    )


def test_snapshot_roundtrip_keeps_numbers_and_hash():
    r = scan()
    doc = report_to_dict(r)
    back = report_from_dict(json.loads(json.dumps(doc)))
    assert back.totals.lowest == r.totals.lowest and back.totals.avg == r.totals.avg
    assert back.totals.net == r.totals.net and back.totals.skewed == r.totals.skewed
    assert snapshot_hash(back) == snapshot_hash(r) == doc["hash"]
    assert {w.code for w in back.warnings} == {w.code for w in r.warnings}


def test_hash_is_stable_and_sensitive():
    assert snapshot_hash(scan()) == snapshot_hash(scan())
    assert snapshot_hash(scan()) != snapshot_hash(scan(window="30d"))


def test_history_latest_and_delta(tmp_path):
    h = History(tmp_path / "hist")
    then, now = scan("demo", "then"), scan()
    # demo reports share a fictional id; history works the same for real ones
    h.save(then)
    prev = h.latest(then.profile.steamid64, "USD", then.opts.games)
    assert prev is not None and prev.totals.lowest == then.totals.lowest
    assert h.latest(then.profile.steamid64, "EUR") is None
    d = delta_from(prev, now)
    assert d.lowest_change == now.totals.lowest - then.totals.lowest
    assert d.days == 30 and d.movers


def test_diff_timeline_lists_movers_added_removed():
    d = compute_diff(scan("demo", "then"), scan())
    assert d.mode == "timeline" and d.span_text == "30 days"
    added = {c.name for c in d.added}
    removed = {c.name for c in d.removed}
    assert "★ Karambit | Doppler (Factory New)" in added
    assert "Desert Eagle | Blaze (Factory New)" in removed
    assert all(c.change > 0 for c in d.gainers) and all(c.change < 0 for c in d.losers)
    assert len(d.gainers) <= 5 and len(d.losers) <= 5


def test_diff_versus_mode():
    d = compute_diff(scan("demo"), scan("demo2"))
    assert d.mode == "versus" and d.shared > 0


def test_redaction_helpers():
    assert mask_name("Kestrel") == "K•••••l"
    assert mask_id("76561197960287930") == "•••• •••• 7930"
    assert value_band(183400, "USD") == "$1k–2.5k"
    assert value_band(50, "USD") == "< $1"
    assert value_band(310000, "EUR") == "€2.5k–5k"
    assert default_level("1", None) == "light"
    assert default_level("1", "1") == "none"
    assert default_level("2", "1") == "full"
    assert default_level("2", "1", is_demo=True) == "none"
    assert at_least("none", "light") == "light" and at_least("full", "light") == "full"


def test_redacted_sidecar_leaks_no_identity(tmp_path):
    r = scan()
    png = tmp_path / "card.png"
    png.write_bytes(b"")
    jpath, apath = write_sidecars(png, r, {"mode": "summary"}, Redaction("full"))
    text = jpath.read_text(encoding="utf-8")
    assert "76561190000000042" not in text and "Kestrel" not in text and "stashcard-demo" not in text
    doc = json.loads(text)
    assert doc["profile"]["persona"] == "Anonymous collector" and doc["totals"]["lowest"].startswith("$")
    assert "Anonymous collector" in apath.read_text(encoding="utf-8")
    light = report_to_dict(r, Redaction("light"))
    assert light["profile"]["steamid64"].endswith("0042") and light["profile"]["vanity"] is None


def test_exports(tmp_path):
    r = scan()
    j = write_json(r, tmp_path / "x.json")
    assert load_snapshot(j).totals.lowest == r.totals.lowest
    c = write_csv(r, tmp_path / "x.csv")
    lines = c.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("game,appid") and len(lines) == len(r.items) + 1
    assert "Steam median" not in lines[0]
    txt = alt_text(r, "summary")
    assert "Total at LOWEST" in txt and "Not affiliated with Valve" in txt
