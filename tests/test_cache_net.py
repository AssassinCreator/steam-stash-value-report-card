import httpx
import pytest
from conftest import dumps, inventory_blob

from stashcard import cache as cache_mod
from stashcard.cache import Cache, human_age
from stashcard.games import GAMES
from stashcard.net import FetchError, Net, OfflineError, PrivateError, RateLimited
from stashcard.profile import ProfileRef
from stashcard.sources import LiveBackend, parse_profile_xml


def test_cache_roundtrip_ttl_and_stale(tmp_path, monkeypatch):
    c = Cache(tmp_path / "c")
    now = [1_000_000.0]
    monkeypatch.setattr(cache_mod.time, "time", lambda: now[0])
    c.put("inventory", "k", {"a": 1})
    assert c.get("inventory", "k").data == {"a": 1}
    now[0] += 11 * 60  # inventory TTL is 10 min
    assert c.get("inventory", "k") is None
    stale = c.get("inventory", "k", ttl=None)
    assert stale.data == {"a": 1} and stale.age == pytest.approx(660)
    assert c.get("inventory", "other") is None


def test_cache_bytes_info_clear(tmp_path):
    c = Cache(tmp_path / "c")
    c.put_bytes("icons", "x/256", b"png")
    assert c.get_bytes("icons", "x/256") == b"png"
    assert c.info()["icons"]["files"] == 1
    assert c.clear("icons") == 1 and c.get_bytes("icons", "x/256") is None


def test_cache_disabled(tmp_path):
    c = Cache(tmp_path / "c", enabled=False)
    c.put("profile", "k", 1)
    assert c.get("profile", "k") is None


def test_human_age():
    assert human_age(30) == "30s" and human_age(600) == "10 min" and human_age(7200) == "2 h"


def _net(handler, **kw):
    sleeps = []
    net = Net(transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return net, sleeps


def test_429_with_long_retry_after_raises_and_blocks_host():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "300"})

    net, _ = _net(handler)
    with pytest.raises(RateLimited) as exc:
        net.get("https://steamcommunity.com/inventory/1/730/2")
    assert "rate-limiting" in str(exc.value) and "stashcard demo" in str(exc.value)
    with pytest.raises(RateLimited):
        net.get("https://steamcommunity.com/market/priceoverview/")
    assert len(calls) == 1  # the second call never left the machine


def test_429_with_short_retry_after_waits_then_succeeds():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    net, sleeps = _net(handler)
    assert net.get("https://api.skinport.com/v1/items").json() == {"ok": True}
    assert 2 in sleeps


def test_honest_user_agent_and_brotli():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, text="ok")

    net, _ = _net(handler)
    net.get("https://api.skinport.com/v1/items", headers={"Accept-Encoding": "br"})
    assert seen["user-agent"].startswith("stashcard/") and "Mozilla" not in seen["user-agent"]
    assert "br" in seen["accept-encoding"]


def test_offline_never_requests():
    net, _ = _net(lambda req: pytest.fail("network used"), offline=True)
    with pytest.raises(OfflineError):
        net.get("https://steamcommunity.com/")


def test_server_errors_become_fetch_errors():
    net, _ = _net(lambda req: httpx.Response(503), retries=0)
    with pytest.raises(FetchError):
        net.get("https://steamcommunity.com/")


PROFILE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><profile>
<steamID64>76561197960287930</steamID64><steamID><![CDATA[Rabscuttle]]></steamID>
<privacyState>public</privacyState><avatarFull><![CDATA[https://avatars.example/a.jpg]]></avatarFull>
<vacBanned>0</vacBanned><tradeBanState>None</tradeBanState><isLimitedAccount>0</isLimitedAccount>
<customURL><![CDATA[gabelogannewell]]></customURL><memberSince>September 12, 2003</memberSince></profile>"""


def test_profile_xml_parsing():
    p = parse_profile_xml(PROFILE_XML)
    assert p.steamid64 == "76561197960287930" and p.persona == "Rabscuttle" and p.vanity == "gabelogannewell"
    assert p.vac_banned is False and p.limited is False and p.member_since.startswith("September")


def test_live_backend_inventory_paginates_and_caches(tmp_path):
    page1 = inventory_blob([("1", "0", "A", 1, {})])
    page1.update(more_items=1, last_assetid="1000")
    page2 = inventory_blob([("2", "0", "B", 1, {})])
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=dumps(page2 if "start_assetid" in str(req.url) else page1))

    net, _ = _net(handler)
    be = LiveBackend(net, Cache(tmp_path / "c"))
    res = be.inventory("76561197960287930", GAMES["cs2"])
    assert len(res.data["assets"]) == 2 and len(calls) == 2
    again = be.inventory("76561197960287930", GAMES["cs2"])
    assert again.age is not None and len(calls) == 2  # served from cache


def test_live_backend_private_inventory(tmp_path):
    net, _ = _net(lambda req: httpx.Response(403, content=b"null"))
    with pytest.raises(PrivateError) as exc:
        LiveBackend(net, Cache(tmp_path / "c")).inventory("76561197960287930", GAMES["cs2"])
    assert "Privacy" in str(exc.value)


def test_live_backend_uses_stale_inventory_when_rate_limited(tmp_path, monkeypatch):
    c = Cache(tmp_path / "c")
    c.put("inventory", "76561197960287930/730/2", inventory_blob([("1", "0", "A", 1, {})]))
    now = cache_mod.time.time()
    monkeypatch.setattr(cache_mod.time, "time", lambda: now + 3600)
    net, _ = _net(lambda req: httpx.Response(429, headers={"Retry-After": "600"}))
    res = LiveBackend(net, c).inventory("76561197960287930", GAMES["cs2"])
    assert res.stale and res.age >= 3600 and res.data["assets"]


def test_priceoverview_parsing_and_currency(tmp_path):
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(
            200, json={"success": True, "lowest_price": "31,90€", "volume": "1,234", "median_price": "32,--€"}
        )

    net, _ = _net(handler)
    data, hit = LiveBackend(net, Cache(tmp_path / "c")).steam_price(
        GAMES["cs2"], "AK-47 | Redline (Field-Tested)", "EUR"
    )
    assert data == {"lowest": 3190, "median_24h": 3200, "volume_24h": 1234} and hit is None
    assert seen["params"]["currency"] == "3" and seen["params"]["appid"] == "730"


def test_skinport_currency_gate(tmp_path):
    net, _ = _net(lambda req: pytest.fail("should not ask Skinport for UAH"))
    be = LiveBackend(net, Cache(tmp_path / "c"))
    assert be.skinport_items(GAMES["cs2"], "UAH") == (None, None)
    assert be.skinport_items(GAMES["steam"], "USD") == (None, None)


def test_profile_by_vanity_uses_cache(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, text=PROFILE_XML)

    net, _ = _net(handler)
    be = LiveBackend(net, Cache(tmp_path / "c"))
    assert be.profile(ProfileRef("x", vanity="gabelogannewell")).steamid64 == "76561197960287930"
    be.profile(ProfileRef("x", vanity="gabelogannewell"))
    assert len(calls) == 1
