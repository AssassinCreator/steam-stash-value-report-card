"""Rich terminal output: the header, step progress, summary tables, warnings and deltas.

``--plain`` (or NO_COLOR) gives colourless ASCII tables with no animation, which also reads
well in screen readers and CI logs.
"""

from __future__ import annotations

import math
import os
import sys

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Column, Table
from rich.text import Text

from stashcard import __version__
from stashcard.diff import Diff
from stashcard.inventory import display_name, short_name
from stashcard.redact import Redaction
from stashcard.report import Report, days_text
from stashcard.scan import Steps

MINT = "#7CF0B6"
AMBER = "#F5B94A"
DIM = "#93A1B5"
BAD = "#FF7A7A"
GOOD = "#6EE7A8"

# 5-row pixel glyphs for the header wordmark, drawn with half blocks.
GLYPHS = {
    "S": [".###", "#...", ".##.", "...#", "###."],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "A": [".##.", "#..#", "####", "#..#", "#..#"],
    "H": ["#..#", "#..#", "####", "#..#", "#..#"],
    "C": [".###", "#...", "#...", "#...", ".###"],
    "R": ["###.", "#..#", "###.", "#.#.", "#..#"],
    "D": ["###.", "#..#", "#..#", "#..#", "###."],
}


def make_console(plain: bool = False, record: bool = False, width: int | None = None) -> Console:
    no_color = plain or bool(os.environ.get("NO_COLOR"))
    try:
        tty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    return Console(
        no_color=no_color,
        color_system=None if plain else "auto",
        # On Windows, only use the legacy console API for a real console window; pipes and CI get plain text.
        legacy_windows=None if tty else False,
        highlight=False,
        emoji=False,
        record=record,
        width=width,
        soft_wrap=False,
    )


def is_plain(console: Console) -> bool:
    return console.no_color or console.color_system is None


def _lerp(a: str, b: str, t: float) -> str:
    ca = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(ca[i] + (cb[i] - ca[i]) * t):02x}" for i in range(3))


def wordmark() -> Text:
    word = "STASHCARD"
    rows = ["", "", "", "", ""]
    for ch in word:
        g = GLYPHS[ch]
        for r in range(5):
            rows[r] += g[r] + "."
    width = len(rows[0])
    rows.append("." * width)
    out = Text()
    for pair in range(3):
        top, bottom = rows[pair * 2], rows[pair * 2 + 1]
        for x in range(width):
            t, b = top[x] == "#", bottom[x] == "#"
            color = _lerp(MINT, AMBER, x / max(1, width - 1))
            ch = "█" if t and b else "▀" if t else "▄" if b else " "
            out.append(ch, style=color)
        out.append("\n")
    return out


def print_header(console: Console, subtitle: str = "value your Steam stash, print the card") -> None:
    if is_plain(console):
        console.print(f"stashcard {__version__} - {subtitle}")
        console.print("Not affiliated with Valve or Steam. Prices are informational.\n")
        return
    mark = wordmark()
    side = Text()
    side.append(f"v{__version__}\n", style=DIM)
    side.append(subtitle + "\n", style="bold")
    side.append("not affiliated with Valve or Steam · prices are informational", style=DIM)
    grid = Table.grid(padding=(0, 3))
    grid.add_column()
    grid.add_column(vertical="middle")
    grid.add_row(mark, side)
    console.print(grid)


# Progress ---------------------------------------------------------------------------------
class RichSteps(Steps):
    """Live step list: a spinner while working, then a tick (or cross) with the result."""

    def __init__(self, console: Console):
        self.console = console
        self.plain = is_plain(console) or not console.is_terminal
        self.progress: Progress | None = None
        self.tasks: dict[str, int] = {}
        if not self.plain:
            self.progress = Progress(
                SpinnerColumn(style=MINT, finished_text=Text("✓", style=GOOD), table_column=Column(width=2)),
                TextColumn("{task.description}", table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis")),
                BarColumn(
                    bar_width=20,
                    style="#223047",
                    complete_style=MINT,
                    finished_style=GOOD,
                    table_column=Column(width=20),
                ),
                MofNCompleteColumn(table_column=Column(width=7, justify="right")),
                TimeElapsedColumn(table_column=Column(width=8)),
                console=console,
                transient=False,
                expand=True,
            )

    def __enter__(self):
        if self.progress:
            self.progress.start()
        return self

    def __exit__(self, *exc):
        if self.progress:
            self.progress.stop()

    def start(self, key, label, total=None):
        if self.progress:
            self.tasks[key] = self.progress.add_task(label, total=total)
        else:
            self.console.print(f"... {label}")

    def update(self, key, *, label=None, completed=None, total=None):
        if self.progress and key in self.tasks:
            kw = {}
            if label:
                kw["description"] = label
            if total is not None:
                kw["total"] = total
            if completed is not None:
                kw["completed"] = completed
            self.progress.update(self.tasks[key], **kw)

    def done(self, key, label=None, ok=True):
        if self.progress and key in self.tasks:
            tid = self.tasks[key]
            task = self.progress.tasks[tid]
            desc = label or task.description
            if not ok:
                desc = f"[{BAD}]✗ {desc}[/]"
            total = task.total if task.total is not None else 1
            self.progress.update(tid, description=desc, total=total, completed=total)
        else:
            mark = "ok " if ok else "ERR"
            self.console.print(f"{mark} {label or key}")

    def wait(self, service, seconds):
        msg = f"{service} asked us to slow down; waiting {seconds:.0f}s"
        if self.progress:
            self.progress.console.print(f"[{AMBER}]{msg}[/]")
        else:
            self.console.print(msg)


# Tables -----------------------------------------------------------------------------------
def _box(console: Console):
    return box.ASCII2 if is_plain(console) else box.SIMPLE_HEAD


def summary_table(report: Report, red: Redaction, width: int = 100) -> Table:
    r = report
    tot = r.totals
    t = Table(box=box.SIMPLE_HEAD, show_footer=True, pad_edge=False, header_style="bold")
    money = (lambda v: red.total(v, r.currency)) if red.bands else r.money
    t.add_column("Game", footer="Total", no_wrap=True)
    t.add_column("Items", justify="right", footer=f"{tot.quantity:,}", no_wrap=True)
    t.add_column("LOWEST", justify="right", footer=money(tot.lowest), style="bold", no_wrap=True)
    t.add_column(f"AVG {r.opts.window}", justify="right", footer=money(tot.avg), no_wrap=True)
    t.add_column("Net Steam", justify="right", footer=money(tot.net), no_wrap=True)
    t.add_column("Net cash", justify="right", footer=money(tot.net_cash) if tot.net_cash else "n/a", no_wrap=True)
    if width >= 90:
        t.add_column("Unpriced", justify="right", footer=str(tot.unpriced), no_wrap=True)
    for g in sorted([g for g in r.games if g.game is not None], key=lambda g: g.lowest, reverse=True):
        if g.error:
            row = [g.game.short, "-", Text("skipped", style=BAD), "-", "-", "-"]
        else:
            row = [
                g.game.short,
                f"{g.quantity:,}",
                money(g.lowest),
                money(g.avg),
                money(g.net),
                money(g.net_cash) if g.net_cash else "n/a",
            ]
        if width >= 90:
            row.append("-" if g.error else str(g.unpriced))
        t.add_row(*row)
    return t


def items_table(report: Report, n: int = 12, width: int = 100) -> Table:
    """Top items. Adapts to the terminal width: optional columns drop out and the item name
    is truncated, so prices are never squeezed."""
    r = report
    wide = width >= 118
    mid = width >= 96
    rows = []
    for i, p in enumerate(r.top(n), 1):
        lo = Text(r.money(p.lowest.amount) if p.lowest else "n/a")
        if p.lowest is not None and p.lowest.fallback:
            lo.append("*", style=AMBER)
        av = Text(r.money(p.avg.amount) if p.avg else "n/a")
        if p.avg is not None and p.avg.fallback:
            av.append("*", style=AMBER)
        med = r.money(p.avg.median) if p.avg is not None and p.avg.median else "-"
        total = (
            r.money(p.total_lowest)
            if p.total_lowest is not None
            else (r.money(p.total_avg) if p.total_avg is not None else "unpriced")
        )
        d = r.days_to_sell(p)
        sell = "locked" if p.item.locked_qty else ("-" if d is None else ("never" if d == math.inf else days_text(d)))
        flags = Text()
        for f in ("unpriced", "locked", "illiquid", "skewed", "spread"):
            if f in p.flags:
                flags.append(f + " ", style=BAD if f == "unpriced" else AMBER)
        flags.rstrip()
        cells = {
            "#": str(i),
            "Item": short_name(display_name(p.item)),
            "Rarity": p.item.rarity_label or "",
            "Qty": f"{p.qty:,}",
            "LOWEST": lo,
            f"AVG {r.opts.window}": av,
            "Median": med,
            "Total": total,
            "Sell in": sell,
            "Flags": flags,
        }
        rows.append(cells)
    cols = (
        ["#", "Item"]
        + (["Rarity"] if wide else [])
        + ["Qty", "LOWEST", f"AVG {r.opts.window}"]
        + (["Median"] if mid else [])
        + ["Total"]
        + (["Sell in"] if wide else [])
        + (["Flags"] if mid else [])
    )
    right = {"#", "Qty", "LOWEST", f"AVG {r.opts.window}", "Median", "Total", "Sell in"}

    def w(col: str) -> int:
        vals = [len(str(c[col])) for c in rows] + [len(col)]
        return min(max(vals), 14 if col == "Rarity" else 40)

    fixed = sum(w(c) for c in cols if c != "Item") + 2 * len(cols)
    item_w = max(12, min(w("Item"), width - fixed - 6))
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, header_style="bold")
    for c in cols:
        t.add_column(
            c,
            justify="right" if c in right else "left",
            no_wrap=True,
            overflow="ellipsis",
            width=item_w if c == "Item" else w(c),
            style=DIM if c in ("#", "Rarity", "Median", "Sell in") else ("bold" if c == "Total" else None),
        )
    for cells in rows:
        t.add_row(*[cells[c] for c in cols])
    return t


def warnings_panel(report: Report, plain: bool) -> Panel | Text:
    lines = Text()
    sev_style = {"high": BAD, "mid": AMBER, "low": DIM}
    for w in report.warnings:
        style = sev_style[w.severity]
        lines.append("! " if w.severity != "low" else "i ", style=style)
        lines.append(w.title, style=f"bold {style}")
        if w.detail:
            lines.append(f"  {w.detail}", style=None if w.severity != "low" else DIM)
        lines.append("\n")
    if not report.warnings:
        lines.append("No warnings.", style=GOOD)
    lines.rstrip()
    if plain:
        return lines
    return Panel(lines, title=f"[{DIM}]honesty checks[/]", title_align="left", border_style="#3A4A63", padding=(0, 1))


def print_report(console: Console, report: Report, red: Redaction | None = None, top: int = 12) -> None:
    red = red or Redaction("none")
    plain = is_plain(console)
    r = report
    name = red.name(r.profile)
    head = Text()
    head.append(name, style="bold")
    url = red.url(r.profile)
    if url:
        head.append(f"  {url}", style=DIM)
    if r.is_demo:
        head.append("  DEMO DATA (fictional profile, dated price snapshot)", style=AMBER)
    console.print()
    console.print(head)
    width = console.width
    console.print(summary_table(r, red, width))
    console.print(Text(f"LOWEST    = {r.lowest_label}", style=DIM))
    console.print(Text(f"AVG       = {r.avg_label}", style=DIM))
    console.print(
        Text(
            f"Net Steam = wallet funds after Valve's 5% + game 10% fees; Net cash = Skinport floor "
            f"after its {r.opts.skinport_fee:.0%} fee",
            style=DIM,
        )
    )
    share = r.sellable_share
    tot = r.totals
    extra = []
    if share is not None:
        extra.append(f"sellable within 7 days: {share:.0%} of value ({r.opts.capture:.0%} capture)")
    if tot.locked_value:
        days = r.unlock_days
        extra.append(
            f"locked now: {red.total(tot.locked_value, r.currency)}"
            + (f", unlocks in {days} days" if days is not None else "")
        )
    if tot.below_min:
        extra.append(f"{tot.below_min} item(s) below min price hidden ({r.money(tot.below_min_value)})")
    if extra:
        console.print(Text(" · ".join(extra), style=DIM))
    vts = [v for v in r.venue_totals if v.stacks]
    if len(vts) > 1:
        money = (lambda v: red.total(v, r.currency)) if red.bands else r.money
        bits = [f"{v.name} {money(v.value)} ({v.coverage(r.totals.lowest):.0%})" for v in vts]
        console.print(Text("At each venue's floor (coverage): " + " · ".join(bits), style=DIM))
        console.print(Text("Full comparison: stashcard markets <profile>", style=DIM))
    fl = fx_line(r)
    if fl is not None:
        console.print(fl)
    console.print()
    if r.items:
        console.print(Text("Top items", style="bold"))
        console.print(items_table(r, top, width))
        if any(p.avg is not None and p.avg.fallback for p in r.top(top)) or any(
            p.lowest is not None and p.lowest.fallback for p in r.top(top)
        ):
            console.print(Text("* fallback source, see the AVG/LOWEST legend above", style=AMBER))
    console.print(warnings_panel(r, plain))
    if r.delta is not None:
        print_delta(console, r, red)


def fx_line(r: Report) -> Text | None:
    if not r.fx_used:
        return None
    return Text(f"FX: {r.fx.label}. Applies to every USD-only venue number above.", style=DIM)


def venue_table(r: Report, red: Redaction) -> Table:
    money = (lambda v: red.total(v, r.currency)) if red.bands else r.money
    rows = [v for v in r.venue_totals if v.stacks]
    best_cash = max((v for v in rows if not v.wallet), key=lambda v: v.net, default=None)
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, header_style="bold")
    for col, just in (
        ("Venue", "left"),
        ("Money", "left"),
        ("Stacks", "right"),
        ("Coverage", "right"),
        ("At floor", "right"),
        ("Fee", "right"),
        ("Net after fee", "right"),
        ("", "left"),
    ):
        t.add_column(col, justify=just, no_wrap=True)
    for v in r.venue_totals:
        if not v.stacks:
            t.add_row(v.name, "wallet" if v.wallet else "cash", "0", "-", Text("no quotes", style=DIM), "-", "-", "")
            continue
        fee = "Valve" if v.fee is None else f"{v.fee:.0%}"
        tag = Text("best cash", style=GOOD) if best_cash is not None and v is best_cash else Text("")
        t.add_row(
            v.name,
            Text("wallet", style=AMBER) if v.wallet else "cash",
            str(v.stacks),
            f"{v.coverage(r.totals.lowest):.0%}",
            money(v.value),
            fee,
            Text(money(v.net), style="bold"),
            tag,
        )
    return t


def print_markets(console: Console, report: Report, red: Redaction | None = None, top: int = 10) -> None:
    """The `markets` mode: inventory at every venue's floor, net after fees, best buy/sell per item."""
    red = red or Redaction("none")
    r = report
    console.print()
    head = Text()
    head.append(red.name(r.profile), style="bold")
    head.append(f"  {r.totals.quantity:,} items \u00b7 {r.currency}", style=DIM)
    if r.is_demo:
        head.append("  DEMO DATA (dated multi-venue snapshot)", style=AMBER)
    console.print(head)
    console.print(Text("Marketplaces", style="bold"))
    console.print(venue_table(r, red))
    route, missing = r.best_cash_route
    line = Text("Best cash route: ", style="bold")
    line.append(red.total(route, r.currency) if red.bands else r.money(route), style=GOOD)
    line.append(f" net if every stack sold at its best cash venue ({missing} stack(s) have no cash quote)", style=DIM)
    console.print(line)
    console.print(
        Text(
            "Coverage = share of your LOWEST value that the venue quotes. Steam money is wallet funds "
            "(cannot be withdrawn); every other venue pays cash. Fees are defaults, override with "
            "--fee venue=rate.",
            style=DIM,
        )
    )
    fl = fx_line(r)
    if fl is not None:
        console.print(fl)
    console.print()
    from stashcard.pricing import venue_label

    width = console.width
    has_cf = any("csfloat" in p.alt_avgs for p in r.top(top))
    rows = []
    for i, p in enumerate(r.top(top), 1):
        buy, sell, steam = p.best_buy(), p.best_sell_cash(), p.venues.get("steam")
        cf = p.alt_avgs.get("csfloat")
        rows.append(
            {
                "#": str(i),
                "Item": short_name(display_name(p.item)),
                "Qty": f"{p.qty:,}",
                "Buy (cheapest)": f"{venue_label(buy.venue)} {r.money(buy.amount)}" if buy else "-",
                "Sell cash (net)": f"{venue_label(sell.venue)} {r.money(sell.net)}" if sell else "-",
                "Steam net": r.money(steam.net) if steam and steam.net is not None else "-",
                "CSFloat avg": ((r.money(cf.amount) + (" skewed" if cf.skewed else "")) if cf else "-"),
            }
        )
    cols = ["#", "Item", "Qty", "Buy (cheapest)", "Sell cash (net)", "Steam net"] + (["CSFloat avg"] if has_cf else [])
    widths = {c: max([len(row[c]) for row in rows] + [len(c)]) for c in cols}
    fixed = sum(w for c, w in widths.items() if c != "Item") + 2 * len(cols) + 6
    if has_cf and fixed + 14 > width:
        cols.remove("CSFloat avg")
        fixed -= widths.pop("CSFloat avg") + 2
    widths["Item"] = max(12, min(widths["Item"], width - fixed))
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, header_style="bold")
    for c in cols:
        t.add_column(
            c,
            justify="right" if c in ("#", "Qty", "Steam net", "CSFloat avg") else "left",
            no_wrap=True,
            overflow="ellipsis",
            width=widths[c],
            style=DIM if c in ("#", "CSFloat avg") else None,
        )
    for row in rows:
        t.add_row(*[row[c] for c in cols])
    console.print(Text("Top items: where to buy and where to sell", style="bold"))
    console.print(t)
    if has_cf:
        console.print(
            Text("CSFloat avg (experimental) = daily sales averages incl. float and sticker premiums.", style=DIM)
        )


def print_delta(console: Console, r: Report, red: Redaction) -> None:
    d = r.delta
    up = d.lowest_change >= 0
    t = Text()
    t.append(f"Since last scan ({d.since}, {d.when}): ", style="bold")
    pct = f"{d.lowest_pct:+.1f}%" if d.lowest_pct is not None else ""
    if red.bands:
        t.append(f"{pct} at LOWEST", style=GOOD if up else BAD)
    else:
        t.append(f"{r.money(d.lowest_change, sign=True)} ({pct}) at LOWEST", style=GOOD if up else BAD)
        t.append(f", {r.money(d.avg_change, sign=True)} at AVG", style=DIM)
    console.print(t)
    for m in d.movers:
        console.print(
            Text(
                f"  {'+' if m.change >= 0 else '-'} {short_name(m.name)}  {r.money(m.change, sign=True)}",
                style=GOOD if m.change >= 0 else BAD,
            )
        )


def print_diff(console: Console, diff: Diff, red_a: Redaction, red_b: Redaction) -> None:
    a, b = diff.old, diff.new
    title = "Then vs now" if diff.mode == "timeline" else "A vs B"
    console.print(Text(title, style="bold"))
    t = Table(box=_box(console), pad_edge=False, header_style="bold")
    t.add_column("")
    t.add_column(
        f"A: {red_a.name(a.profile)}" if diff.mode == "versus" else a.generated_at.strftime("%Y-%m-%d"), justify="right"
    )
    t.add_column(
        f"B: {red_b.name(b.profile)}" if diff.mode == "versus" else b.generated_at.strftime("%Y-%m-%d"), justify="right"
    )
    t.add_column("Change", justify="right")
    for label, va, vb in (
        ("LOWEST", a.totals.lowest, b.totals.lowest),
        (f"AVG {b.opts.window}", a.totals.avg, b.totals.avg),
        ("Net Steam", a.totals.net, b.totals.net),
    ):
        ch = vb - va
        t.add_row(
            label,
            red_a.total(va, a.currency),
            red_b.total(vb, b.currency),
            Text(
                b.money(ch, sign=True)
                if not (red_a.bands or red_b.bands)
                else (f"{ch / va * 100:+.1f}%" if va else "-"),
                style=GOOD if ch >= 0 else BAD,
            ),
        )
    console.print(t)
    if diff.mode == "timeline":
        for label, rows, style in (("Gainers", diff.gainers, GOOD), ("Losers", diff.losers, BAD)):
            if rows:
                console.print(Text(label, style=f"bold {style}"))
                for c in rows:
                    console.print(Text(f"  {short_name(c.name):<44} {b.money(c.change, sign=True):>12}"))
        if diff.added:
            console.print(
                Text(f"Added ({len(diff.added)}): " + ", ".join(short_name(c.name) for c in diff.added[:6]), style=DIM)
            )
        if diff.removed:
            console.print(
                Text(
                    f"Removed ({len(diff.removed)}): " + ", ".join(short_name(c.name) for c in diff.removed[:6]),
                    style=DIM,
                )
            )
    else:
        console.print(
            Text(
                f"{diff.shared} items in both · {len(diff.removed)} only in A · " f"{len(diff.added)} only in B",
                style=DIM,
            )
        )


def error(console: Console, message: str, hint: str | None = None) -> None:
    console.print(Text("error: ", style=f"bold {BAD}") + Text(message))
    if hint:
        console.print(Text(hint, style=DIM))


def note(console: Console, message: str) -> None:
    console.print(Text(message, style=DIM))


def stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty() and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


__all__ = ["Group", "RichSteps", "make_console", "print_header", "print_report", "print_diff", "error", "note"]
