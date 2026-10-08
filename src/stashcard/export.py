"""JSON / CSV exports, card sidecars (card.json) and alt text (card.alt.txt)."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from stashcard.diff import Diff
from stashcard.inventory import display_name, short_name
from stashcard.markets import VENUES
from stashcard.redact import Redaction
from stashcard.report import Report
from stashcard.snapshot import report_to_dict

CSV_FIELDS = (
    [
        "game",
        "appid",
        "contextid",
        "market_hash_name",
        "quantity",
        "tradable_quantity",
        "rarity",
        "currency",
        "lowest_unit",
        "lowest_source",
        "lowest_window",
        "lowest_fallback",
        "avg_unit",
        "avg_metric",
        "avg_source",
        "avg_window",
        "avg_fallback",
        "avg_median",
        "avg_max",
        "avg_sales",
        "net_steam_unit",
        "net_skinport_unit",
        "total_lowest",
        "total_avg",
        "total_net_steam",
        "daily_sales",
        "daily_sales_source",
        "days_to_sell",
        "flags",
    ]
    + [f"{v}_{part}" for v in VENUES if VENUES[v].kind == "floor" for part in ("floor", "net")]
    + [
        "csfloat_avg",
        "best_buy_venue",
        "best_cash_sell_venue",
        "fx_usd_rate",
        "fx_date",
    ]
)


def _major(v: int | None) -> str:
    return "" if v is None else f"{v / 100:.2f}"


def write_json(report: Report, path: str | Path, redaction: Redaction | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report, redaction), ensure_ascii=False, indent=2), "utf-8")
    return path


def write_csv(report: Report, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for p in report.items:
            lo, av = p.lowest, p.avg
            days = report.days_to_sell(p)
            w.writerow(
                {
                    "game": p.item.game.short,
                    "appid": p.item.game.appid,
                    "contextid": p.item.game.contextid,
                    "market_hash_name": p.item.market_hash_name,
                    "quantity": p.qty,
                    "tradable_quantity": p.item.tradable_qty,
                    "rarity": p.item.rarity_label or "",
                    "currency": report.currency,
                    "lowest_unit": _major(lo.amount if lo else None),
                    "lowest_source": lo.source if lo else "",
                    "lowest_window": lo.window if lo else "",
                    "lowest_fallback": int(lo.fallback) if lo else "",
                    "avg_unit": _major(av.amount if av else None),
                    "avg_metric": av.metric if av else "",
                    "avg_source": av.source if av else "",
                    "avg_window": av.window if av else "",
                    "avg_fallback": int(av.fallback) if av else "",
                    "avg_median": _major(av.median if av else None),
                    "avg_max": _major(av.max if av else None),
                    "avg_sales": av.volume if av and av.volume else "",
                    "net_steam_unit": _major(p.unit_net),
                    "net_skinport_unit": _major(p.unit_cash(report.opts.skinport_fee)),
                    "total_lowest": _major(p.total_lowest),
                    "total_avg": _major(p.total_avg),
                    "total_net_steam": _major(p.total_net),
                    "daily_sales": "" if p.daily_volume is None else f"{p.daily_volume:.2f}",
                    "daily_sales_source": p.daily_volume_source or "",
                    "days_to_sell": "" if days is None else ("inf" if days == math.inf else f"{days:.1f}"),
                    "flags": " ".join(sorted(p.flags)),
                    **{
                        f"{v}_{part}": _major(getattr(p.venues[v], "amount" if part == "floor" else "net"))
                        if v in p.venues
                        else ""
                        for v in VENUES
                        if VENUES[v].kind == "floor"
                        for part in ("floor", "net")
                    },
                    "csfloat_avg": _major(p.alt_avgs["csfloat"].amount) if "csfloat" in p.alt_avgs else "",
                    "best_buy_venue": (p.best_buy().venue if p.best_buy() else ""),
                    "best_cash_sell_venue": (p.best_sell_cash().venue if p.best_sell_cash() else ""),
                    "fx_usd_rate": f"{report.fx.rate:.6g}" if report.fx_used else "",
                    "fx_date": report.fx.date if report.fx_used else "",
                }
            )
    return path


def alt_text(report: Report, mode: str, redaction: Redaction | None = None) -> str:
    red = redaction or Redaction("none")
    r, tot = report, report.totals
    who = red.name(r.profile) + (" (demo data, fictional profile)" if r.is_demo else "")
    stamp = r.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    kind = {
        "summary": "report card",
        "game": "game inventory card",
        "grid": "inventory picture",
        "receipt": "receipt-style card",
        "markets": "marketplace comparison card",
    }.get(mode, "card")
    games = ", ".join(g.game.short for g in r.games if g.game is not None)
    parts = [
        f"Stashcard {kind} for {who}, {stamp}, {r.currency}.",
        f"Total at LOWEST ({r.lowest_label}): {red.total(tot.lowest, r.currency)}.",
        f"AVG ({r.avg_label}): {red.total(tot.avg, r.currency)}.",
        f"Net if sold on Steam after fees (wallet funds): {red.total(tot.net, r.currency)}"
        + (f"; on Skinport after its fee (cash): {red.total(tot.net_cash, r.currency)}." if tot.net_cash else "."),
        f"{tot.quantity:,} items in {tot.stacks} stacks across {games}.",
    ]
    vts = [v for v in r.venue_totals if v.stacks]
    if len(vts) > 1:
        parts.append(
            "By marketplace (floor / net after fee): "
            + "; ".join(
                f"{v.name} {'wallet' if v.wallet else 'cash'} {red.total(v.value, r.currency)} / "
                f"{red.total(v.net, r.currency)}"
                for v in vts
            )
            + "."
        )
        route, _ = r.best_cash_route
        parts.append(f"Best cash route: {red.total(route, r.currency)}.")
    if r.fx_used:
        parts.append(f"USD-only venues converted at {r.fx.label}.")
    top = r.top(5)
    if top:
        rows = []
        for p in top:
            value = r.money(p.total_lowest) if p.total_lowest is not None else "unpriced"
            rows.append(f"{short_name(display_name(p.item))} x{p.qty} {value}")
        parts.append("Top items: " + "; ".join(rows) + ".")
    warn = [
        w.title
        for w in r.warnings
        if w.code in ("unpriced", "liquidity", "skewed", "spread", "locked", "nonmarketable", "ratelimit")
    ]
    if warn:
        parts.append("Warnings: " + ", ".join(warn) + ".")
    if r.delta is not None and r.delta.lowest_pct is not None:
        parts.append(f"Change since {r.delta.since}: {r.delta.lowest_pct:+.1f}% at LOWEST.")
    parts.append("Not affiliated with Valve or Steam. Prices are informational.")
    return " ".join(parts)


def diff_alt_text(diff: Diff, red_a: Redaction, red_b: Redaction) -> str:
    a, b = diff.old, diff.new
    if diff.mode == "timeline":
        pct = f"{diff.lowest_pct:+.1f}%" if diff.lowest_pct is not None else "n/a"
        s = (
            f"Stashcard then-vs-now card for {red_b.name(b.profile)}: {a.generated_at:%Y-%m-%d} to "
            f"{b.generated_at:%Y-%m-%d} ({diff.span_text}). Change at LOWEST {pct}. "
        )
        if diff.gainers:
            s += "Top gainer: " + short_name(diff.gainers[0].name) + ". "
        if diff.losers:
            s += "Top loser: " + short_name(diff.losers[0].name) + ". "
        s += f"{len(diff.added)} items added, {len(diff.removed)} removed."
    else:
        s = (
            f"Stashcard A vs B card: {red_a.name(a.profile)} at {red_a.total(a.totals.lowest, a.currency)} versus "
            f"{red_b.name(b.profile)} at {red_b.total(b.totals.lowest, b.currency)} (LOWEST). "
            f"{diff.shared} items in both."
        )
    return s + " Not affiliated with Valve or Steam. Prices are informational."


def write_sidecars(
    png: str | Path, report: Report, render: dict, redaction: Redaction | None = None, alt: str | None = None
) -> tuple[Path, Path]:
    """card.png -> card.json (inputs + outputs, re-renderable offline) and card.alt.txt."""
    png = Path(png)
    doc = report_to_dict(report, redaction, extra={"render": render})
    jpath = png.with_suffix(".json")
    jpath.write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")
    apath = png.with_name(png.stem + ".alt.txt")
    apath.write_text((alt or alt_text(report, render.get("mode", "summary"), redaction)) + "\n", "utf-8")
    return jpath, apath


def write_diff_sidecars(
    png: str | Path, diff: Diff, render: dict, red_a: Redaction, red_b: Redaction
) -> tuple[Path, Path]:
    png = Path(png)
    doc = {
        "kind": "diff",
        "mode": diff.mode,
        "render": render,
        "old": report_to_dict(diff.old, red_a),
        "new": report_to_dict(diff.new, red_b),
    }
    jpath = png.with_suffix(".json")
    jpath.write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")
    apath = png.with_name(png.stem + ".alt.txt")
    apath.write_text(diff_alt_text(diff, red_a, red_b) + "\n", "utf-8")
    return jpath, apath
