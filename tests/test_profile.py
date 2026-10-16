import pytest

from stashcard.profile import (
    ProfileInputError,
    decode_friend_code,
    encode_friend_code,
    parse_profile,
    steamid64_to_steamid2,
    steamid64_to_steamid3,
)

GABE = "76561197960287930"  # account id 22202


@pytest.mark.parametrize(
    "text",
    [
        "https://steamcommunity.com/profiles/76561197960287930",
        "http://steamcommunity.com/profiles/76561197960287930/",
        "steamcommunity.com/profiles/76561197960287930/?xml=1",
        "www.steamcommunity.com/profiles/76561197960287930#top",
        "76561197960287930",
        "  76561197960287930  ",
        "STEAM_0:0:11101",
        "STEAM_1:0:11101",
        "[U:1:22202]",
        "U:1:22202",
        "22202",  # numeric friend code from Steam's Add a Friend page
        "https://steamcommunity.com/user/hjqp",
        "https://s.team/p/hjqp",
        "s.team/p/hjqp/ABCDEFGH",
    ],
)
def test_resolves_to_steamid64(text):
    assert parse_profile(text).steamid64 == GABE


@pytest.mark.parametrize(
    "text,vanity",
    [
        ("https://steamcommunity.com/id/gabelogannewell", "gabelogannewell"),
        ("steamcommunity.com/id/gabelogannewell/inventory/", "gabelogannewell"),
        ("gabelogannewell", "gabelogannewell"),
        ("some_name-42", "some_name-42"),
        ("demo", "demo"),
    ],
)
def test_vanity(text, vanity):
    ref = parse_profile(text)
    assert ref.vanity == vanity and ref.steamid64 is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://steamcommunity.com/market/listings/730/foo",
        "765611979602879301234",
        "https://steamcommunity.com/profiles/123",
        "not a profile!",
        "s.team/p/xyz!",
    ],
)
def test_rejects_with_message(text):
    with pytest.raises(ProfileInputError) as exc:
        parse_profile(text)
    assert len(str(exc.value)) > 20


def test_friend_code_roundtrip():
    # Verified live on 2026-10-29: steamcommunity.com/user/hjqp redirects to /id/GabeLoganNewell.
    assert decode_friend_code("hjqp") == 22202
    assert encode_friend_code(22202) == "hjqp"
    assert decode_friend_code("bcdf-ghjk") == int("01234567", 16)


def test_id_conversions():
    assert steamid64_to_steamid2(GABE) == "STEAM_1:0:11101"
    assert steamid64_to_steamid3(GABE) == "[U:1:22202]"
