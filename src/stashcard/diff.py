"""Then-vs-now and A-vs-B comparisons between two Reports."""

from __future__ import annotations

from dataclasses import dataclass, field

from stashcard.inventory import display_name
from stashcard.models import Item, PricedItem
from stashcard.report import Report


@dataclass
class ItemChange:
    item: Item
    qty_old: int
    qty_new: int
    value_old: int  # total at LOWEST (falls back to AVG when there is no floor)
    value_new: int

    @property
    def change(self) -> int:
        return self.value_new - self.value_old

    @property
    def pct(self) -> float | None:
        return None if not self.value_old else self.change / self.value_old * 100

    @property
    def name(self) -> str:
        return display_name(self.item)


@dataclass
class Diff:
    old: Report
    new: Report
    mode: str  # "timeline" (same profile, two moments) or "versus" (two profiles)
    gainers: list[ItemChange] = field(default_factory=list)
    losers: list[ItemChange] = field(default_factory=list)
    added: list[ItemChange] = field(default_factory=list)
    removed: list[ItemChange] = field(default_factory=list)
    shared: int = 0

    @property
    def days(self) -> float:
        return abs((self.new.generated_at - self.old.generated_at).total_seconds()) / 86400

    @property
    def lowest_change(self) -> int:
        return self.new.totals.lowest - self.old.totals.lowest

    @property
    def avg_change(self) -> int:
        return self.new.totals.avg - self.old.totals.avg

    @property
    def lowest_pct(self) -> float | None:
        return None if not self.old.totals.lowest else self.lowest_change / self.old.totals.lowest * 100

    @property
    def avg_pct(self) -> float | None:
        return None if not self.old.totals.avg else self.avg_change / self.old.totals.avg * 100

    @property
    def span_text(self) -> str:
        return span_text(self.days)

    @property
    def comparable(self) -> bool:
        return self.old.currency == self.new.currency


def span_text(days: float) -> str:
    if days < 1 / 24:
        return "minutes"
    if days < 1:
        return f"{round(days * 24)} h"
    d = round(days)
    if d < 60:
        return f"{d} day{'s' if d != 1 else ''}"
    return f"{round(d / 30)} months"


def _value(p: PricedItem) -> int:
    if p.total_lowest is not None:
        return p.total_lowest
    return p.total_avg or 0


def compute_diff(old: Report, new: Report, *, mode: str | None = None, top: int = 5) -> Diff:
    if mode is None:
        mode = "timeline" if old.profile.steamid64 == new.profile.steamid64 else "versus"
    a = {p.item.key: p for p in old.items}
    b = {p.item.key: p for p in new.items}
    d = Diff(old, new, mode)
    moved: list[ItemChange] = []
    for key in a.keys() & b.keys():
        pa, pb = a[key], b[key]
        moved.append(ItemChange(pb.item, pa.qty, pb.qty, _value(pa), _value(pb)))
    d.shared = len(moved)
    d.gainers = sorted((c for c in moved if c.change > 0), key=lambda c: c.change, reverse=True)[:top]
    d.losers = sorted((c for c in moved if c.change < 0), key=lambda c: c.change)[:top]
    d.added = sorted(
        (ItemChange(b[k].item, 0, b[k].qty, 0, _value(b[k])) for k in b.keys() - a.keys()),
        key=lambda c: c.value_new,
        reverse=True,
    )
    d.removed = sorted(
        (ItemChange(a[k].item, a[k].qty, 0, _value(a[k]), 0) for k in a.keys() - b.keys()),
        key=lambda c: c.value_old,
        reverse=True,
    )
    return d


@dataclass
class Delta:
    """The 'since last scan' line in the terminal and on the report card."""

    since: str  # ISO date of the previous scan
    days: float
    lowest_change: int
    avg_change: int
    lowest_pct: float | None
    avg_pct: float | None
    movers: list[ItemChange]

    @property
    def when(self) -> str:
        if self.days < 1:
            return f"{span_text(self.days)} ago"
        return f"{span_text(self.days)} ago"


def delta_from(old: Report, new: Report) -> Delta:
    d = compute_diff(old, new, mode="timeline", top=3)
    movers = sorted(d.gainers + d.losers, key=lambda c: abs(c.change), reverse=True)[:3]
    return Delta(
        old.generated_at.strftime("%Y-%m-%d"), d.days, d.lowest_change, d.avg_change, d.lowest_pct, d.avg_pct, movers
    )
