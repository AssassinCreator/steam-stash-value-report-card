"""Turn whatever the user pasted into a Steam profile reference.

Accepted forms:
  * https://steamcommunity.com/id/<vanity>  (scheme, www, trailing slash and query optional)
  * https://steamcommunity.com/profiles/<steamid64>
  * a bare SteamID64 (17 digits starting with 7656119)
  * STEAM_0:X:Y / STEAM_1:X:Y (SteamID2)
  * [U:1:N] or U:1:N (SteamID3)
  * steamcommunity.com/user/<code> and s.team/p/<code> (friend invite codes)
  * a bare vanity name
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STEAMID64_BASE = 76561197960265728
_FRIEND_ALPHABET = "bcdfghjkmnpqrtvw"  # hex digits 0-f, as used by s.team/p/ invite codes


class ProfileInputError(ValueError):
    """Raised with a friendly, actionable message."""


@dataclass(frozen=True)
class ProfileRef:
    raw: str
    steamid64: str | None = None
    vanity: str | None = None

    @property
    def is_demo(self) -> bool:
        return self.vanity in ("demo", "demo2") and self.steamid64 is None

    def display(self) -> str:
        return self.steamid64 or f"id/{self.vanity}"


def account_id_to_steamid64(account_id: int) -> str:
    return str(STEAMID64_BASE + account_id)


def steamid64_to_account_id(steamid64: str | int) -> int:
    return int(steamid64) - STEAMID64_BASE


def steamid64_to_steamid2(steamid64: str | int) -> str:
    acc = steamid64_to_account_id(steamid64)
    return f"STEAM_1:{acc & 1}:{acc >> 1}"


def steamid64_to_steamid3(steamid64: str | int) -> str:
    return f"[U:1:{steamid64_to_account_id(steamid64)}]"


def decode_friend_code(code: str) -> int:
    """Decode an invite code such as ``hjqp`` or ``bcdf-ghjk`` into an account id."""
    clean = code.lower().replace("-", "")
    if not clean or any(ch not in _FRIEND_ALPHABET for ch in clean):
        raise ProfileInputError(
            f"{code!r} does not look like a Steam friend code. Paste the profile URL instead "
            "(Steam > your name > Profile > right click > Copy page URL)."
        )
    hex_digits = "".join(f"{_FRIEND_ALPHABET.index(ch):x}" for ch in clean)
    return int(hex_digits, 16)


def encode_friend_code(account_id: int) -> str:
    return "".join(_FRIEND_ALPHABET[int(h, 16)] for h in f"{account_id:x}")


def _valid64(value: str) -> bool:
    if not (value.isdigit() and len(value) == 17 and value.startswith("7656119")):
        return False
    return 0 < int(value) - STEAMID64_BASE < 2**32


_URL = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?(?P<host>steamcommunity\.com|s\.team)/(?P<kind>id|profiles|user|p)/(?P<val>[^/?#\s]+)",
    re.IGNORECASE,
)
_STEAM2 = re.compile(r"^STEAM_[0-5]:([01]):(\d{1,10})$", re.IGNORECASE)
_STEAM3 = re.compile(r"^\[?U:1:(\d{1,10})\]?$", re.IGNORECASE)
_VANITY = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


def parse_profile(text: str) -> ProfileRef:
    raw = (text or "").strip().strip("<>\"'")
    if not raw:
        raise ProfileInputError("Paste a Steam profile URL, SteamID64, STEAM_0:1:..., [U:1:...] or vanity name.")

    m = _URL.match(raw)
    if m:
        kind, val = m.group("kind").lower(), m.group("val")
        if kind == "profiles":
            if not _valid64(val):
                raise ProfileInputError(f"{val!r} is not a valid SteamID64 (17 digits starting with 7656119).")
            return ProfileRef(raw, steamid64=val)
        if kind == "id":
            if not _VANITY.match(val):
                raise ProfileInputError(f"{val!r} is not a valid custom profile name.")
            return ProfileRef(raw, vanity=val)
        # /user/<code> or s.team/p/<code>
        return ProfileRef(raw, steamid64=account_id_to_steamid64(decode_friend_code(val)))

    if "steamcommunity.com" in raw.lower() or "s.team" in raw.lower():
        raise ProfileInputError(
            "That link is not a profile link. Use a URL like https://steamcommunity.com/id/<name> "
            "or https://steamcommunity.com/profiles/<steamid64>."
        )

    if raw.isdigit():
        if _valid64(raw):
            return ProfileRef(raw, steamid64=raw)
        if len(raw) <= 10 and int(raw) > 0:
            return ProfileRef(raw, steamid64=account_id_to_steamid64(int(raw)))
        raise ProfileInputError(f"{raw!r} is not a valid SteamID64 (17 digits starting with 7656119).")

    m = _STEAM2.match(raw)
    if m:
        y, z = int(m.group(1)), int(m.group(2))
        return ProfileRef(raw, steamid64=account_id_to_steamid64(z * 2 + y))

    m = _STEAM3.match(raw)
    if m:
        return ProfileRef(raw, steamid64=account_id_to_steamid64(int(m.group(1))))

    if raw.lower() in ("demo", "demo2"):
        return ProfileRef(raw, vanity=raw.lower())

    if _VANITY.match(raw):
        return ProfileRef(raw, vanity=raw)

    raise ProfileInputError(
        f"Could not read {raw!r} as a Steam profile. Try the full profile URL, a SteamID64, "
        "STEAM_0:1:12345, [U:1:24691] or your custom URL name."
    )
