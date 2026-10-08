import json

from stashcard.cli import main
from stashcard.config import Secrets, Settings, mask_secret


def run(*args):
    return main(list(args))


def test_demo_all_offline(tmp_path, capsys):
    out = tmp_path / "cards"
    assert run("demo", "--offline", "--mode", "all", "--out-dir", str(out), "-q", "--no-preview") == 0
    names = sorted(p.name for p in out.glob("*.png"))
    assert names == sorted(
        f"stashcard-demo-{m}.png" for m in ("summary", "game", "grid", "receipt", "markets", "then-vs-now", "a-vs-b")
    )
    for png in out.glob("*.png"):
        assert png.with_suffix(".json").exists() and (out / f"{png.stem}.alt.txt").exists()


def test_card_and_value_with_exports(tmp_path, capsys):
    png = tmp_path / "grid.png"
    assert (
        run(
            "card",
            "demo",
            "--mode",
            "grid",
            "--offline",
            "-o",
            str(png),
            "--no-preview",
            "--plain",
            "--json",
            str(tmp_path / "v.json"),
            "--csv",
            str(tmp_path / "v.csv"),
            "-c",
            "EUR",
        )
        == 0
    )
    assert png.exists() and (tmp_path / "v.csv").exists()
    doc = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    assert doc["options"]["currency"] == "EUR" and doc["kind"] == "snapshot"
    out = capsys.readouterr().out
    assert "LOWEST" in out and "\x1b[" not in out  # --plain has no colour codes
    assert run("value", "demo", "--offline", "-q") == 0
    assert capsys.readouterr().out.strip().startswith("$")


def test_render_rerenders_sidecar_offline(tmp_path):
    png = tmp_path / "s.png"
    assert run("card", "demo", "--offline", "-o", str(png), "-q", "--no-preview") == 0
    again = tmp_path / "again.png"
    assert (
        run(
            "render",
            str(png.with_suffix(".json")),
            "--offline",
            "-o",
            str(again),
            "-q",
            "--no-preview",
            "--theme",
            "paper",
        )
        == 0
    )
    assert again.exists()


def test_diff_demo(tmp_path):
    png = tmp_path / "d.png"
    assert run("diff", "demo@then", "demo", "--offline", "-o", str(png), "-q", "--no-preview") == 0
    doc = json.loads(png.with_suffix(".json").read_text(encoding="utf-8"))
    assert doc["kind"] == "diff" and doc["mode"] == "timeline"


def test_bad_profile_exit_code(capsys):
    assert run("value", "https://steamcommunity.com/market/", "--offline") == 1
    assert "not a profile link" in capsys.readouterr().out


def test_offline_live_profile_explains(capsys):
    assert run("value", "76561197960287930", "--offline") == 3
    assert "Offline" in capsys.readouterr().out


def test_config_me_and_secrets_are_masked(capsys):
    assert run("config", "me", "76561197960287930") == 0
    assert Settings.load().me == "76561197960287930"
    assert run("config", "api-key", "ABCDEF0123456789XYZ1") == 0
    out = capsys.readouterr().out
    assert "ABCDEF0123456789XYZ1" not in out and "XYZ1" in out
    assert Secrets.load()["api_key"] == "ABCDEF0123456789XYZ1"
    assert run("config", "show") == 0
    assert "ABCDEF0123456789" not in capsys.readouterr().out
    assert run("config", "forget-secrets") == 0 and Secrets.load() == {}
    assert mask_secret(None) == "not set"


def test_currencies_and_cache(capsys):
    assert run("currencies") == 0
    assert "EUR" in capsys.readouterr().out
    assert run("cache", "info") == 0


def test_markets_command_and_flags(tmp_path, capsys):
    png = tmp_path / "m.png"
    assert (
        run(
            "markets",
            "demo",
            "--offline",
            "-c",
            "EUR",
            "--markets",
            "all",
            "--fee",
            "waxpeer=5%",
            "-o",
            str(png),
            "--no-preview",
            "--plain",
            "--csv",
            str(tmp_path / "m.csv"),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Best cash route" in out and "Waxpeer" in out and "FX: 1 USD =" in out
    assert png.exists()
    header = (tmp_path / "m.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "waxpeer_floor" in header and "lisskins_net" in header and "fx_usd_rate" in header
    assert run("value", "demo", "--offline", "--lowest-from", "best", "-q") == 0
    assert run("value", "demo", "--offline", "--markets", "steam,nope") == 1
    assert "Unknown marketplace" in capsys.readouterr().out
