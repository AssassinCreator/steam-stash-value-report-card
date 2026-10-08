"""Join Steam's inventory JSON (assets + descriptions) into stacks of identical items."""

from __future__ import annotations

import re

from stashcard.games import Game
from stashcard.models import Item

# "Tradable/Marketable After Nov 05, 2026 (7:00:00) GMT" (classic trade hold) and the CS2 trade
# protection wording. TODO(verify): the exact CS2 trade-protection text was not observed live here.
_HOLD = re.compile(
    r"(?:(?:Tradable|Marketable)(?:/Marketable)?\s+After|Trade[- ]?Protect\w*\s+until|Protected\s+until)\s+(.+)$",
    re.I,
)


def _tag(desc: dict, category: str) -> dict | None:
    for tag in desc.get("tags") or []:
        if tag.get("category") == category:
            return tag
    return None


def _trade_hold(desc: dict) -> str | None:
    for block in desc.get("owner_descriptions") or []:
        m = _HOLD.search(re.sub(r"<[^>]+>", "", str(block.get("value", ""))))
        if m:
            return m.group(1).strip()
    return None


def join_inventory(raw: dict, game: Game, *, tradable_only: bool = False) -> list[Item]:
    """Return one Item per market_hash_name, summing asset amounts.

    Assets join to descriptions on (classid, instanceid). Assets with no matching description
    are skipped (Steam occasionally omits them). Non-tradable copies are dropped when
    ``tradable_only`` is set.
    """
    descs: dict[tuple[str, str], dict] = {}
    for d in raw.get("descriptions") or []:
        descs[(str(d.get("classid")), str(d.get("instanceid", "0")))] = d

    stacks: dict[str, Item] = {}
    order: list[str] = []
    for asset in raw.get("assets") or []:
        d = descs.get((str(asset.get("classid")), str(asset.get("instanceid", "0"))))
        if d is None:
            continue
        try:
            amount = int(asset.get("amount", 1) or 1)
        except (TypeError, ValueError):
            amount = 1
        tradable = bool(int(d.get("tradable", 0) or 0))
        if tradable_only and not tradable:
            continue
        name = d.get("market_hash_name") or d.get("market_name") or d.get("name") or "Unknown item"
        item = stacks.get(name)
        if item is None:
            rarity = _tag(d, "Rarity")
            exterior = _tag(d, "Exterior")
            quality = _tag(d, "Quality")
            item = Item(
                game=game,
                market_hash_name=name,
                name=d.get("name") or name,
                type=d.get("type") or "",
                icon_url=d.get("icon_url_large") or d.get("icon_url") or "",
                name_color=(d.get("name_color") or "").lstrip("#") or None,
                rarity=rarity.get("localized_tag_name") if rarity else None,
                rarity_color=((rarity.get("color") or "").lstrip("#") or None) if rarity else None,
                exterior=exterior.get("localized_tag_name") if exterior else None,
                quality=quality.get("localized_tag_name") if quality else None,
                marketable=bool(int(d.get("marketable", 0) or 0)),
                commodity=bool(int(d.get("commodity", 0) or 0)),
            )
            stacks[name] = item
            order.append(name)
        item.quantity += amount
        if tradable:
            item.tradable_qty += amount
        else:
            hold = _trade_hold(d)
            if hold and not item.trade_hold:
                item.trade_hold = hold
        # A stack is marketable if any copy is. Trade-held copies count: they become marketable later.
        item.marketable = item.marketable or bool(int(d.get("marketable", 0) or 0)) or bool(item.trade_hold)
        if asset.get("assetid"):
            item.asset_ids.append(str(asset["assetid"]))
    return [stacks[n] for n in order]


def display_name(item: Item) -> str:
    """Steam Community items prefix the market name with the appid ("730-IDF (Foil)")."""
    if item.game.appid == 753 and item.name:
        return item.name
    return item.market_hash_name


_WEAR_SHORT = {
    "Factory New": "FN",
    "Minimal Wear": "MW",
    "Field-Tested": "FT",
    "Well-Worn": "WW",
    "Battle-Scarred": "BS",
}


def short_name(name: str) -> str:
    """Shorten CS2 wear suffixes: "AWP | Asiimov (Field-Tested)" -> "AWP | Asiimov (FT)"."""
    for long, short in _WEAR_SHORT.items():
        name = name.replace(f"({long})", f"({short})")
    return name
