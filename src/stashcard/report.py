"""Aggregate priced items into a Report: totals per game, honesty warnings and source notes."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from stashcard.fx import FxRate
from stashcard.games import Game
from stashcard.inventory import display_name, short_name
from stashcard.markets import VENUES
from stashcard.models import Item, PricedItem, Profile
from stashcard.money import format_money
from stashcard.pricing import Notice, ScanOptions

SPREAD_LIMIT = 0.5
SELL_HORIZON_DAYS = 7


@dataclass
class Totals:
    lowest: int = 0
    avg: int = 0
    net: int = 0  # Steam wallet funds after Valve's fees, at the Steam floor
    net_cash: int = 0  # Skinport cash after its seller fee, at the Skinport floor
    stacks: int = 0
    quantity: int = 0
    priced: int = 0
    unpriced: int = 0
    skewed: int = 0
    nonmarketable: int = 0  # stacks
    nonmarketable_qty: int = 0
    lowest_missing: int = 0
    avg_missing: int = 0
    net_missing: int = 0
    cash_missing: int = 0
    locked_qty: int = 0
    locked_value: int = 0
    below_min: int = 0
    below_min_value: int = 0

    def add(self, p: PricedItem, cash_fee: float) -> None:
        self.stacks += 1
        self.quantity += p.qty
        if p.priced:
            self.priced += 1
        else:
            self.unpriced += 1
        if "skewed" in p.flags:
            self.skewed += 1
        if p.total_lowest is not None:
            self.lowest += p.total_lowest
        else:
            self.lowest_missing += 1
        if p.total_avg is not None:
            self.avg += p.total_avg
        else:
            self.avg_missing += 1
        if p.total_net is not None:
            self.net += p.total_net
        else:
            self.net_missing += 1
        cash = p.total_cash(cash_fee)
        if cash is not None:
            self.net_cash += cash
        else:
            self.cash_missing += 1
        if p.item.locked_qty:
            self.locked_qty += p.item.locked_qty
            self.locked_value += p.locked_value

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class GameTotals(Totals):
    game: Game | None = None
    error: str | None = None


@dataclass
class Warning:
    code: str  # ratelimit unpriced nonmarketable liquidity spread skewed fallback locked cache source info
    title: str
    detail: str = ""
    items: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        return {
            "ratelimit": "high",
            "unpriced": "high",
            "liquidity": "mid",
            "spread": "mid",
            "skewed": "mid",
            "locked": "mid",
        }.get(self.code, "low")

    def to_dict(self) -> dict:
        return {"code": self.code, "title": self.title, "detail": self.detail, "items": self.items}


@dataclass
class Report:
    profile: Profile
    opts: ScanOptions
    items: list[PricedItem]  # marketable, filtered, sorted by value desc
    nonmarketable: list[Item]
    games: list[GameTotals]
    totals: Totals
    warnings: list[Warning]
    notices: list[Notice]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_demo: bool = False
    label: str | None = None  # e.g. "then" for a historical demo snapshot
    delta: object | None = None  # diff.Delta, attached after the scan
    fx: FxRate | None = None  # USD -> report currency, when USD-only venues were converted

    @property
    def fx_used(self) -> bool:
        return self.fx is not None and not self.fx.identity and any(v.fx for p in self.items for v in p.venues.values())

    @property
    def venue_totals(self) -> list[VenueTotals]:
        return venue_totals(self)

    @property
    def best_cash_route(self) -> tuple[int, int]:
        """(total, stacks without any cash quote): every stack sold at its best cash venue."""
        total, missing = 0, 0
        for p in self.items:
            best = p.best_sell_cash()
            if best is None:
                missing += 1
            else:
                total += best.net * p.qty
        return total, missing

    @property
    def currency(self) -> str:
        return self.opts.currency

    def money(self, minor: int | None, **kw) -> str:
        return format_money(minor, self.currency, **kw)

    def top(self, n: int = 10) -> list[PricedItem]:
        return self.items[:n]

    @property
    def lowest_label(self) -> str:
        return source_summary(self.items, "lowest")

    @property
    def avg_label(self) -> str:
        return source_summary(self.items, "avg")

    @property
    def primary_lowest(self) -> str:
        return primary_label(self.items, "lowest")

    @property
    def primary_avg(self) -> str:
        return primary_label(self.items, "avg")

    def days_to_sell(self, p: PricedItem) -> float | None:
        return p.days_to_sell(self.opts.capture)

    @property
    def sellable_share(self) -> float | None:
        """Share of LOWEST value that could plausibly sell within 7 days at the capture rate."""
        total = sum(p.total_lowest or 0 for p in self.items)
        if not total:
            return None
        fast = 0
        for p in self.items:
            d = self.days_to_sell(p)
            if d is not None and d <= SELL_HORIZON_DAYS and not p.item.locked_qty:
                fast += p.total_lowest or 0
        return fast / total

    @property
    def liquidity_grade(self) -> str:
        share = self.sellable_share
        if share is None:
            return "-"
        for limit, grade in ((0.9, "A"), (0.75, "B"), (0.55, "C"), (0.35, "D")):
            if share >= limit:
                return grade
        return "F"

    @property
    def unlock_days(self) -> int | None:
        """Days until the last locked stack unlocks, relative to the report time."""
        dates = [p.item.unlock_at for p in self.items if p.item.locked_qty and p.item.unlock_at]
        if not dates:
            return None
        return max(0, math.ceil((max(dates) - self.generated_at).total_seconds() / 86400))

    def game_totals(self, game: Game) -> GameTotals | None:
        return next((g for g in self.games if g.game == game), None)

    def subset(self, game: Game) -> Report:
        """A report restricted to one game (used by the game card)."""
        items = [p for p in self.items if p.item.game == game]
        nonm = [i for i in self.nonmarketable if i.game == game]
        gt = self.game_totals(game) or GameTotals(game=game)
        totals = Totals(**{k: v for k, v in gt.__dict__.items() if k in Totals.__dataclass_fields__})
        names = {display_name(p.item) for p in items} | {display_name(i) for i in nonm}
        warns = []
        for w in self.warnings:
            if w.items:
                keep = [n for n in w.items if n in names]
                if keep:
                    warns.append(Warning(w.code, _retitle(w, len(keep)), w.detail, keep))
            elif w.code in ("ratelimit", "source", "cache"):
                warns.append(w)
        return Report(
            self.profile,
            self.opts,
            items,
            nonm,
            [gt],
            totals,
            warns,
            self.notices,
            self.generated_at,
            self.is_demo,
            self.label,
            fx=self.fx,
        )


@dataclass
class VenueTotals:
    venue: str
    name: str
    wallet: bool
    fee: float | None
    stacks: int = 0
    quantity: int = 0
    value: int = 0  # at this venue's floor
    net: int = 0  # after this venue's seller fee
    covered_lowest: int = 0  # LOWEST value of the stacks this venue quotes

    def coverage(self, total_lowest: int) -> float:
        return self.covered_lowest / total_lowest if total_lowest else 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def venue_totals(r: Report) -> list[VenueTotals]:
    """Inventory value at each enabled floor venue. Venues cover different items, so each row
    carries its coverage; compare rows with similar coverage."""
    out = []
    for key in r.opts.markets:
        v = VENUES.get(key)
        if v is None or v.kind != "floor":
            continue
        t = VenueTotals(key, v.name, v.wallet, r.opts.fee_for(key))
        for p in r.items:
            q = p.venues.get(key)
            if q is None:
                continue
            t.stacks += 1
            t.quantity += p.qty
            t.value += q.amount * p.qty
            t.net += (q.net or 0) * p.qty
            t.covered_lowest += p.total_lowest or 0
        out.append(t)
    return out


def _retitle(w: Warning, n: int) -> str:
    titles = {
        "unpriced": f"{n} unpriced",
        "nonmarketable": f"{n} not marketable",
        "liquidity": f"{n} illiquid",
        "spread": f"{n} wide spread",
        "skewed": f"{n} skewed",
        "fallback": f"{n} fallback",
        "locked": f"{n} locked",
    }
    return titles.get(w.code, w.title)


def source_summary(items: list[PricedItem], which: str) -> str:
    """'Steam floor · now' or 'Skinport avg · 7d (+9 Steam median · 24h)'."""
    labels = Counter()
    for p in items:
        q = getattr(p, which)
        if q is not None:
            labels[q.label] += 1
    if not labels:
        return "no data"
    (main, _), *rest = labels.most_common()
    if not rest:
        return main
    extra = ", ".join(f"{n} {lab}" for lab, n in rest)
    return f"{main} (+{extra})"


def primary_label(items: list[PricedItem], which: str) -> str:
    labels = Counter(getattr(p, which).label for p in items if getattr(p, which) is not None)
    return labels.most_common(1)[0][0] if labels else "no data"


def build_report(
    profile: Profile,
    opts: ScanOptions,
    priced: list[PricedItem],
    nonmarketable: list[Item],
    games: list[GameTotals],
    notices: list[Notice],
    *,
    is_demo: bool = False,
    generated_at: datetime | None = None,
    label: str | None = None,
    apply_min: bool = True,
    fx: FxRate | None = None,
) -> Report:
    totals = Totals()
    by_game = {g.game: g for g in games}
    kept: list[PricedItem] = []
    for p in priced:
        if apply_min and opts.min_price and p.priced and p.unit_value < opts.min_price:
            totals.below_min += p.qty
            totals.below_min_value += p.sort_value
            continue
        kept.append(p)
    kept.sort(key=lambda p: (p.sort_value, p.qty, p.item.market_hash_name), reverse=True)

    for p in kept:
        gt = by_game.setdefault(p.item.game, GameTotals(game=p.item.game))
        totals.add(p, opts.fee_for("skinport"))
        gt.add(p, opts.fee_for("skinport"))
    for it in nonmarketable:
        gt = by_game.setdefault(it.game, GameTotals(game=it.game))
        gt.nonmarketable += 1
        gt.nonmarketable_qty += it.quantity
        totals.nonmarketable += 1
        totals.nonmarketable_qty += it.quantity

    report = Report(
        profile,
        opts,
        kept,
        nonmarketable,
        list(by_game.values()),
        totals,
        [],
        notices,
        is_demo=is_demo,
        label=label,
        fx=fx,
    )
    if generated_at:
        report.generated_at = generated_at
    report.warnings = build_warnings(report)
    return report


def _names(rows: list[PricedItem]) -> list[str]:
    return [display_name(p.item) for p in rows]


def _short(p: PricedItem) -> str:
    return short_name(display_name(p.item))


def build_warnings(r: Report) -> list[Warning]:
    items, opts = r.items, r.opts
    out: list[Warning] = []
    for n in r.notices:
        if n.kind == "ratelimit":
            out.append(Warning("ratelimit", "Rate limited", n.text))
    for g in r.games:
        if g.error:
            out.append(Warning("source", f"{g.game.short} skipped", g.error))

    unpriced = [p for p in items if "unpriced" in p.flags]
    if unpriced:
        out.append(
            Warning(
                "unpriced",
                f"{len(unpriced)} unpriced",
                "No floor and no sales from any source. Counted as unpriced, never as zero.",
                _names(unpriced),
            )
        )

    if r.nonmarketable:
        qty = sum(i.quantity for i in r.nonmarketable)
        out.append(
            Warning(
                "nonmarketable",
                f"{len(r.nonmarketable)} not marketable",
                f"{qty:,} item(s) cannot be sold on the market (medals, coins, gems). "
                "Listed separately, not valued.",
                [display_name(i) for i in r.nonmarketable],
            )
        )

    illiquid = [p for p in items if "illiquid" in p.flags]
    if illiquid:
        details = []
        for p in illiquid[:5]:
            days = r.days_to_sell(p)
            eta = "no recent sales" if days == math.inf else f"~{_days(days)} to sell"
            details.append(f"{_short(p)} x{p.qty} vs {p.daily_volume or 0:,.1f}/day ({eta})")
        out.append(
            Warning(
                "liquidity",
                f"{len(illiquid)} illiquid",
                "You hold more than one day of sales: " + "; ".join(details),
                _names(illiquid),
            )
        )

    skewed = [p for p in items if "skewed" in p.flags]
    if skewed:
        details = []
        for p in skewed[:5]:
            a = p.avg
            details.append(
                f"{_short(p)} mean {r.money(a.raw_mean or a.amount)} vs median {r.money(a.median)}"
                + (f", max {r.money(a.max)}" if a.max and a.max > 5 * (a.median or 0) else "")
            )
        out.append(
            Warning(
                "skewed",
                f"{len(skewed)} skewed",
                "AVG pulled by outlier sales (mean vs median > 25 % or max > 5x median): " + "; ".join(details),
                _names(skewed),
            )
        )

    wide = [p for p in items if "spread" in p.flags]
    if wide:
        details = [f"{_short(p)} {p.spread:.0%}" for p in wide[:5] if p.spread is not None]
        out.append(
            Warning(
                "spread",
                f"{len(wide)} wide spread",
                f"Floor and average disagree by more than {SPREAD_LIMIT:.0%}: " + "; ".join(details),
                _names(wide),
            )
        )

    locked = [p for p in items if "locked" in p.flags]
    if locked:
        details = []
        for p in locked[:4]:
            when = p.item.unlock_at
            if when:
                days = max(0, math.ceil((when - r.generated_at).total_seconds() / 86400))
                details.append(f"{_short(p)} x{p.item.locked_qty} unlocks in {days} d")
            else:
                details.append(f"{_short(p)} x{p.item.locked_qty}")
        out.append(
            Warning(
                "locked",
                f"{len(locked)} locked",
                f"Trade-locked now: {r.money(r.totals.locked_value)}. " + "; ".join(details),
                _names(locked),
            )
        )

    fb = [p for p in items if "fallback_avg" in p.flags]
    if fb:
        labels = Counter(p.avg.label for p in fb if p.avg)
        what = ", ".join(f"{lab} for {n} item(s)" for lab, n in labels.most_common())
        out.append(
            Warning(
                "fallback",
                f"{len(fb)} AVG fallback",
                f"No {opts.window} sales average from the primary source; using {what}. " "Labelled on every row.",
                _names(fb),
            )
        )

    fbl = [p for p in items if "fallback_lowest" in p.flags]
    if fbl:
        labels = Counter(p.lowest.label for p in fbl if p.lowest)
        what = ", ".join(f"{n} {lab}" for lab, n in labels.most_common())
        out.append(
            Warning("fallback", f"{len(fbl)} floor fallback", f"Primary floor unavailable; using {what}.", _names(fbl))
        )

    for n in r.notices:
        if n.kind in ("cache", "source", "info"):
            out.append(Warning(n.kind, n.kind.title(), n.text))
    return out


def _days(d: float | None) -> str:
    if d is None:
        return "?"
    if d == math.inf:
        return "never"
    if d < 1:
        return "<1 day"
    if d < 60:
        return f"{d:.0f} days" if d >= 2 else "1 day"
    return f"{d / 30:.0f} months"


def days_text(d: float | None) -> str:
    return _days(d)
