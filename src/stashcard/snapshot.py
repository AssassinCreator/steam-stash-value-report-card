"""Snapshots: a Report as JSON (sidecars, exports, local history) and back again.

A snapshot carries everything a card needs, so ``stashcard render card.json`` can redraw it
offline. Local history lives in the user data dir as one JSON file per scan and never leaves
the machine.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from stashcard import __version__
from stashcard.fx import FxRate
from stashcard.games import GAMES, Game, parse_game
from stashcard.markets import DEFAULT_MARKETS
from stashcard.models import Item, PricedItem, Profile, Quote, VenueQuote
from stashcard.paths import data_dir
from stashcard.pricing import Notice, ScanOptions
from stashcard.redact import Redaction
from stashcard.report import GameTotals, Report, build_report

SCHEMA = 1


def _game_ref(g: Game) -> str:
    return g.key if not g.custom else g.context


def _item_dict(it: Item) -> dict:
    return {
        "game": _game_ref(it.game),
        "market_hash_name": it.market_hash_name,
        "name": it.name,
        "type": it.type,
        "icon_url": it.icon_url,
        "name_color": it.name_color,
        "rarity": it.rarity,
        "rarity_color": it.rarity_color,
        "exterior": it.exterior,
        "quality": it.quality,
        "quantity": it.quantity,
        "tradable_qty": it.tradable_qty,
        "marketable": it.marketable,
        "commodity": it.commodity,
        "trade_hold": it.trade_hold,
    }


def _item_from(d: dict) -> Item:
    return Item(
        game=parse_game(d["game"]),
        market_hash_name=d["market_hash_name"],
        name=d.get("name") or d["market_hash_name"],
        type=d.get("type", ""),
        icon_url=d.get("icon_url", ""),
        name_color=d.get("name_color"),
        rarity=d.get("rarity"),
        rarity_color=d.get("rarity_color"),
        exterior=d.get("exterior"),
        quality=d.get("quality"),
        quantity=int(d.get("quantity", 0)),
        tradable_qty=int(d.get("tradable_qty", d.get("quantity", 0))),
        marketable=bool(d.get("marketable", True)),
        commodity=bool(d.get("commodity", False)),
        trade_hold=d.get("trade_hold"),
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def snapshot_hash(report: Report) -> str:
    """Short, stable fingerprint of what was valued and how. Printed on every card."""
    core = {
        "t": _iso(report.generated_at),
        "o": report.opts.to_dict(),
        "i": sorted(
            (p.item.key, p.qty, p.lowest.amount if p.lowest else None, p.avg.amount if p.avg else None)
            for p in report.items
        ),
        "n": sorted((i.key, i.quantity) for i in report.nonmarketable),
    }
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:10]


def report_to_dict(report: Report, redaction: Redaction | None = None, extra: dict | None = None) -> dict:
    red = redaction or Redaction("none")
    prof = red.profile(report.profile)
    totals = report.totals.to_dict()
    if red.bands:
        for k in ("lowest", "avg", "net", "net_cash", "locked_value", "below_min_value"):
            totals[k] = red.total(totals[k], report.currency)
    doc = {
        "stashcard": __version__,
        "schema": SCHEMA,
        "kind": "snapshot",
        "generated_at": _iso(report.generated_at),
        "hash": snapshot_hash(report),
        "label": report.label,
        "is_demo": report.is_demo,
        "redaction": red.level,
        "profile": {k: v for k, v in prof.__dict__.items()},
        "options": report.opts.to_dict(),
        "sources": {"lowest": report.lowest_label, "avg": report.avg_label},
        "fx": report.fx.to_dict() if report.fx else None,
        "venue_totals": [
            {
                **v.to_dict(),
                **(
                    {
                        "value": red.total(v.value, report.currency),
                        "net": red.total(v.net, report.currency),
                        "covered_lowest": None,
                    }
                    if red.bands
                    else {}
                ),
            }
            for v in report.venue_totals
        ],
        "totals": totals,
        "games": [
            {
                "game": _game_ref(g.game),
                "name": g.game.name,
                "error": g.error,
                **({k: v for k, v in g.to_dict().items() if k not in ("game", "error")} if not red.bands else {}),
            }
            for g in report.games
        ],
        "items": [
            {
                **_item_dict(p.item),
                "lowest": p.lowest.to_dict() if p.lowest else None,
                "avg": p.avg.to_dict() if p.avg else None,
                "daily_volume": p.daily_volume,
                "daily_volume_source": p.daily_volume_source,
                "steam_floor": p.steam_floor,
                "skinport_floor": p.skinport_floor,
                "venues": {k: v.to_dict() for k, v in p.venues.items()},
                "alt_avgs": {k: q.to_dict() for k, q in p.alt_avgs.items()},
                "flags": sorted(p.flags),
            }
            for p in report.items
        ],
        "nonmarketable": [_item_dict(i) for i in report.nonmarketable],
        "warnings": [w.to_dict() for w in report.warnings],
        "notices": [{"kind": n.kind, "text": n.text} for n in report.notices],
    }
    if extra:
        doc.update(extra)
    return doc


def report_from_dict(doc: dict) -> Report:
    if doc.get("kind") != "snapshot":
        raise ValueError("Not a Stashcard snapshot (missing kind: snapshot).")
    o = doc.get("options", {})
    opts = ScanOptions(
        games=[parse_game(g) for g in o.get("games", list(GAMES))],
        currency=o.get("currency", "USD"),
        source=o.get("source", "both"),
        window=o.get("window", "7d"),
        min_price=int(o.get("min_price", 0)),
        tradable_only=bool(o.get("tradable_only", False)),
        trimmed_avg=bool(o.get("trimmed_avg", False)),
        capture=float(o.get("capture", 0.10)),
        skinport_fee=float(o.get("skinport_fee", 0.08)),
        markets=list(o.get("markets") or DEFAULT_MARKETS),
        lowest_from=o.get("lowest_from"),
        fees={k: float(v) for k, v in (o.get("fees") or {}).items() if v is not None},
    )
    pd = doc.get("profile", {})
    profile = Profile(**{k: v for k, v in pd.items() if k in Profile.__dataclass_fields__})
    priced = []
    for d in doc.get("items", []):
        p = PricedItem(
            _item_from(d),
            lowest=Quote.from_dict(d.get("lowest")),
            avg=Quote.from_dict(d.get("avg")),
            daily_volume=d.get("daily_volume"),
            daily_volume_source=d.get("daily_volume_source"),
            steam_floor=d.get("steam_floor"),
            skinport_floor=d.get("skinport_floor"),
            flags=set(d.get("flags", [])),
            venues={k: VenueQuote.from_dict(v) for k, v in (d.get("venues") or {}).items()},
            alt_avgs={k: Quote.from_dict(v) for k, v in (d.get("alt_avgs") or {}).items() if v},
        )
        priced.append(p)
    nonm = [_item_from(d) for d in doc.get("nonmarketable", [])]
    games = [GameTotals(game=parse_game(g["game"]), error=g.get("error")) for g in doc.get("games", [])]
    notices = [Notice(n["kind"], n["text"]) for n in doc.get("notices", [])]
    # The min-price filter was applied when the snapshot was taken; do not apply it twice.
    return build_report(
        profile,
        opts,
        priced,
        nonm,
        games,
        notices,
        is_demo=bool(doc.get("is_demo")),
        generated_at=_parse_iso(doc["generated_at"]),
        label=doc.get("label"),
        apply_min=False,
        fx=FxRate.from_dict(doc.get("fx")),
    )


def load_snapshot(path: str | Path) -> Report:
    doc = json.loads(Path(path).read_text("utf-8"))
    return report_from_dict(doc)


# ------------------------------------------------------------------------------------------
# Local scan history
# ------------------------------------------------------------------------------------------


class History:
    KEEP = 200

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir() / "history"

    def _dir(self, steamid64: str) -> Path:
        return self.root / "".join(ch for ch in steamid64 if ch.isdigit())

    def save(self, report: Report) -> Path:
        d = self._dir(report.profile.steamid64)
        d.mkdir(parents=True, exist_ok=True)
        stamp = report.generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = d / f"{stamp}-{report.currency}.json"
        path.write_text(json.dumps(report_to_dict(report), ensure_ascii=False), "utf-8")
        files = sorted(d.glob("*.json"))
        for old in files[: max(0, len(files) - self.KEEP)]:
            old.unlink(missing_ok=True)
        return path

    def entries(self, steamid64: str) -> list[Path]:
        d = self._dir(steamid64)
        return sorted(d.glob("*.json")) if d.exists() else []

    def latest(
        self, steamid64: str, currency: str, games: list[Game] | None = None, before: datetime | None = None
    ) -> Report | None:
        want = sorted(_game_ref(g) for g in games) if games else None
        for path in reversed(self.entries(steamid64)):
            if not path.stem.endswith(f"-{currency}"):
                continue
            try:
                doc = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if want is not None and sorted(doc.get("options", {}).get("games", [])) != want:
                continue
            if before is not None and _parse_iso(doc["generated_at"]) >= before:
                continue
            try:
                return report_from_dict(doc)
            except (KeyError, ValueError):
                continue
        return None
