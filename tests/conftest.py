import json
import os

os.environ.setdefault("ALLOW_UNSUPPORTED_OS", "1")

import httpx
import pytest

from stashcard.games import GAMES
from stashcard.models import Item


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own settings/cache/history folder and no real network."""
    monkeypatch.setenv("STASHCARD_HOME", str(tmp_path / "home"))
    for var in ("STASHCARD_STEAM_API_KEY", "STASHCARD_STEAM_LOGIN_SECURE", "STASHCARD_PREVIEW", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)

    def refuse(*args, **kwargs):  # pragma: no cover - only hit if a test forgets a mock
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    return tmp_path


def make_item(name="AK-47 | Redline (Field-Tested)", game="cs2", qty=1, **kw) -> Item:
    return Item(
        game=GAMES[game],
        market_hash_name=name,
        name=kw.pop("display", name),
        quantity=qty,
        tradable_qty=kw.pop("tradable_qty", qty),
        **kw,
    )


def inventory_blob(rows):
    """rows: (classid, instanceid, name, amount, extra-desc-dict)."""
    assets, descs, seen = [], [], set()
    for n, (classid, instance, name, amount, extra) in enumerate(rows):
        assets.append(
            {
                "appid": 730,
                "contextid": "2",
                "assetid": str(1000 + n),
                "classid": classid,
                "instanceid": instance,
                "amount": str(amount),
            }
        )
        if (classid, instance) not in seen:
            seen.add((classid, instance))
            descs.append(
                {
                    "classid": classid,
                    "instanceid": instance,
                    "market_hash_name": name,
                    "name": name,
                    "type": "Classified Rifle",
                    "icon_url": "abc",
                    "marketable": 1,
                    "tradable": 1,
                    **extra,
                }
            )
    return {"assets": assets, "descriptions": descs, "total_inventory_count": len(assets), "success": 1}


def dumps(obj) -> bytes:
    return json.dumps(obj).encode()
