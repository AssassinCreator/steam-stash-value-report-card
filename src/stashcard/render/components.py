"""Shared card pieces: header, profile block, stat panels, item rows, warnings, trust footer."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timezone

from PIL import Image

from stashcard import __version__
from stashcard.inventory import display_name, short_name
from stashcard.models import PricedItem
from stashcard.money import format_money
from stashcard.redact import Redaction
from stashcard.render.canvas import Canvas
from stashcard.render.icons import accent_of, avatar_image, load_icon
from stashcard.render.theme import Theme, mix
from stashcard.report import Report, days_text
from stashcard.snapshot import snapshot_hash

W = 1200
M = 48  # outer margin

FLAG_TEXT = {
    "unpriced": "UNPRICED",
    "illiquid": "ILLIQUID",
    "skewed": "SKEWED",
    "spread": "SPREAD",
    "locked": "LOCKED",
    "fallback_avg": "AVG FALLBACK",
    "fallback_lowest": "FLOOR FALLBACK",
}
FLAG_ORDER = ["unpriced", "locked", "illiquid", "skewed", "spread", "fallback_avg", "fallback_lowest"]


@dataclass
class Ctx:
    report: Report
    theme: Theme
    backend: object | None = None
    redaction: Redaction = field(default_factory=Redaction)
    _icons: dict = field(default_factory=dict)

    @property
    def cur(self) -> str:
        return self.report.currency

    def money(self, minor: int | None, **kw) -> str:
        return format_money(minor, self.cur, **kw)

    def total(self, minor: int | None, **kw) -> str:
        """Totals honour value bands under full redaction."""
        return self.redaction.total(minor, self.cur, **kw)

    def icon(self, p_or_item, size: int = 256) -> tuple[Image.Image, bool]:
        item = getattr(p_or_item, "item", p_or_item)
        key = (item.key, size)
        if key not in self._icons:
            self._icons[key] = load_icon(self.backend, item, self.theme, size)
        return self._icons[key]

    @property
    def hash(self) -> str:
        return snapshot_hash(self.report)


def flag_color(t: Theme, flag: str) -> str:
    if flag in ("unpriced", "ratelimit"):
        return t.bad
    if flag in ("illiquid", "skewed", "spread", "locked", "liquidity"):
        return t.warn
    return t.dim


def sorted_flags(p: PricedItem) -> list[str]:
    return [f for f in FLAG_ORDER if f in p.flags]


# Header ---------------------------------------------------------------------------------
def header(c: Canvas, ctx: Ctx, kind: str, *, width: int = W, y: float = 34) -> float:
    t = ctx.theme
    r = ctx.report
    c.rrect((M, y, M + 11, y + 24), 2, fill=t.accent)
    x = M + 22
    x += c.text((x, y + 19), "stashcard", "mono_xbold", 21, t.text)
    x += 12
    c.text((x, y + 18), "/ " + kind, "mono_med", 13, t.dim, tracking=1.2)
    stamp = r.generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    right = width - M
    if r.is_demo:
        right -= c.pill(right, y + 12, "DEMO DATA", color=t.accent2, size=11, anchor="r") + 10
    if r.label == "then":
        right -= c.pill(right, y + 12, "SNAPSHOT", color=t.dim, size=11, anchor="r") + 10
    c.label((right, y + 17), f"{stamp}  ·  {ctx.cur}", 12, t.dim, anchor="rs")
    c.line(M, y + 46, width - M, y + 46, t.rule, 1)
    return y + 46


# Profile --------------------------------------------------------------------------------
def profile_facts(ctx: Ctx) -> list[tuple[str, str]]:
    p, t, red = ctx.report.profile, ctx.theme, ctx.redaction
    facts: list[tuple[str, str]] = []
    if red.level != "full":
        yrs = p.member_years
        if yrs is not None:
            facts.append((f"MEMBER {yrs} YR{'S' if yrs != 1 else ''}", t.dim))
        if p.level is not None:
            facts.append((f"LEVEL {p.level}", t.dim))
        if p.games_owned:
            facts.append((f"{p.games_owned:,} GAMES", t.dim))
    if p.vac_banned:
        facts.append(("VAC BAN ON RECORD", t.bad))
    elif p.vac_banned is False:
        facts.append(("NO VAC BANS", t.good))
    if p.trade_ban and p.trade_ban.lower() not in ("none", ""):
        facts.append((f"TRADE BAN: {p.trade_ban.upper()}", t.bad))
    if p.limited:
        facts.append(("LIMITED ACCOUNT", t.warn))
    return facts


def profile_block(
    c: Canvas, ctx: Ctx, x: float, y: float, *, avatar: float = 112, max_w: float = 640, name_size: float = 44
) -> float:
    t, p, red = ctx.theme, ctx.report.profile, ctx.redaction
    av = avatar_image(ctx.backend, p, t, red.level, 256)
    c.shadow((x, y, x + avatar, y + avatar), 22, blur=14, alpha=90 if t.dark else 40, dy=6)
    c.paste(av, (x, y, x + avatar, y + avatar), radius=22)
    c.rrect((x, y, x + avatar, y + avatar), 22, outline=mix(t.border, t.text, 0.1), width=1)
    tx = x + avatar + 24
    name = red.name(p)
    c.text((tx, y + 44), name, "display", name_size, t.text, max_w=max_w)
    url = red.url(p)
    sub = url if url else f"SteamID {red.steamid(p)}"
    if red.level == "none" and url:
        sub = f"{url}"
    c.text((tx, y + 74), sub, "mono", 14, t.dim, max_w=max_w)
    fx = tx
    for text, color in profile_facts(ctx):
        w = c.measure(text, "mono_med", 11, tracking=0.44) + 18
        if fx + w > tx + max_w:
            break
        fx += c.pill(fx, y + 100, text, color=color, size=11) + 8
    return y + avatar


# Panels ---------------------------------------------------------------------------------
def fit_size(c: Canvas, text: str, role: str, size: float, max_w: float, min_size: float = 18) -> float:
    while size > min_size and c.measure(text, role, size) > max_w:
        size -= 1
    return size


def stat_panel(
    c: Canvas,
    ctx: Ctx,
    box,
    title: str,
    value: str,
    source: str,
    note: str = "",
    *,
    accent: str | None = None,
    value_size: float = 40,
) -> None:
    t = ctx.theme
    x0, y0, x1, y1 = box
    c.panel(box, 18)
    if accent:
        c.rrect((x0 + 1, y0 + 18, x0 + 4, y0 + 46), 1.5, fill=accent)
    c.label((x0 + 24, y0 + 34), title, 12, t.dim, max_w=x1 - x0 - 48)
    size = fit_size(c, value, "mono_xbold", value_size, x1 - x0 - 48)
    c.text((x0 + 24, y0 + 34 + 16 + size * 0.86), value, "mono_xbold", size, t.text)
    yy = y1 - (40 if note else 22)
    c.text((x0 + 24, yy), source, "mono_med", 12.5, accent or t.accent, max_w=x1 - x0 - 48)
    if note:
        c.text((x0 + 24, yy + 19), note, "mono", 12, t.faint if t.dark else t.dim, max_w=x1 - x0 - 48)


def icon_tile(c: Canvas, ctx: Ctx, p_or_item, box, radius: float = 10, strip: bool = True) -> None:
    t = ctx.theme
    item = getattr(p_or_item, "item", p_or_item)
    x0, y0, x1, y1 = box
    accent = accent_of(item, t)
    bg_top = mix(accent, t.panel, 0.80 if t.dark else 0.86)
    bg_bottom = mix(accent, t.panel, 0.92 if t.dark else 0.95)
    c.vgradient(box, bg_top, bg_bottom, radius=radius)
    im, real = ctx.icon(item, 256 if (x1 - x0) > 72 else 128)
    c.paste_contain(im, box, pad=(x1 - x0) * (0.08 if real else 0.0))
    if strip:
        c.rrect((x0, y1 - 3, x1, y1), 1.5, fill=accent)
    c.rrect(box, radius, outline=mix(accent, t.panel, 0.6), width=1)


def item_row(
    c: Canvas, ctx: Ctx, p: PricedItem, x: float, y: float, w: float, *, icon: float = 44, show_avg: bool = True
) -> None:
    """One compact item row: icon, name, rarity text + qty + flags, LOWEST total and AVG."""
    t = ctx.theme
    icon_tile(c, ctx, p, (x, y, x + icon, y + icon), radius=9)
    tx = x + icon + 14
    right = x + w
    total = (
        ctx.money(p.total_lowest)
        if p.total_lowest is not None
        else (ctx.money(p.total_avg) if p.total_avg is not None else "unpriced")
    )
    val_w = c.measure(total, "mono_bold", 15)
    c.text((right, y + 18), total, "mono_bold", 15, t.text if p.priced else t.bad, anchor="rs")
    if show_avg:
        if p.avg is not None and p.lowest is not None:
            sub = f"avg {ctx.money(p.total_avg)}"
            if p.avg.median:
                sub += f" · med {ctx.money(p.avg.median * p.qty)}"
        elif p.lowest is None and p.avg is not None:
            sub = p.avg.short
        else:
            sub = p.lowest.short if p.lowest else "no data"
        sub_w = c.measure(sub, "mono", 11)
        c.text((right, y + 37), sub, "mono", 11, t.dim, anchor="rs", max_w=200)
    else:
        sub_w = 0
    name_w = right - tx - max(val_w, min(sub_w, 200)) - 16
    role = "mono_med" if t.mono_names else "sans_semi"
    c.text((tx, y + 18), short_name(display_name(p.item)), role, 15, t.text, max_w=name_w, middle=True)
    bits = []
    rl = p.item.rarity_label
    if rl:
        bits.append(rl)
    bits.append(f"x{p.qty:,}")
    meta = " · ".join(bits)
    mx = tx + c.text((tx, y + 37), meta, "mono", 11, t.dim, max_w=name_w)
    for f in sorted_flags(p)[:2]:
        if f.startswith("fallback"):
            continue
        txt = FLAG_TEXT[f]
        need = c.measure(txt, "mono_bold", 9.5, tracking=0.38) + 12
        if mx + 8 + need > tx + name_w:
            break
        mx += 8 + c.pill(mx + 8, y + 33, txt, color=flag_color(t, f), size=9.5, role="mono_bold", pad_x=6, h=16)


# Warnings and footer --------------------------------------------------------------------
def warning_chips(c: Canvas, ctx: Ctx, x: float, y: float, max_x: float) -> float:
    t, r = ctx.theme, ctx.report
    chips: list[tuple[str, str]] = []
    tot = r.totals
    for w in r.warnings:
        if w.code in ("unpriced", "liquidity", "skewed", "spread", "nonmarketable", "ratelimit"):
            color = flag_color(t, w.code if w.code != "nonmarketable" else "info")
            chips.append((w.title.upper(), color))
        elif w.code == "locked":
            days = r.unlock_days
            label = f"LOCKED {ctx.total(tot.locked_value)}" + (f" · {days}D" if days is not None else "")
            chips.append((label, t.warn))
        elif w.code == "fallback" and "AVG" in w.title:
            chips.append((w.title.upper(), t.dim))
    if not chips:
        chips.append(("NO WARNINGS", t.good))
    cx = x
    for i, (text, color) in enumerate(chips):
        need = c.measure(text, "mono_bold", 11, tracking=0.44) + 22
        more = len(chips) - i
        if cx + need > max_x - (70 if more > 1 else 0):
            c.pill(cx, y, f"+{more} MORE", color=t.dim, size=11, role="mono_bold")
            break
        cx += c.pill(cx, y, text, color=color, size=11, role="mono_bold") + 8
    return y + 14


def trust_footer(c: Canvas, ctx: Ctx, y: float, *, x0: float = M, x1: float = W - M) -> float:
    """Sources, windows, currency, UTC time, honesty counts and the snapshot hash. Every card."""
    t, r = ctx.theme, ctx.report
    tot = r.totals
    c.line(x0, y, x1, y, t.rule, 1)
    col = t.faint if t.dark else t.dim
    stamp = r.generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line1 = f"LOWEST {r.lowest_label}  |  AVG {r.avg_label}"
    if r.opts.trimmed_avg:
        line1 += "   (trimmed: highest and lowest sales removed)"
    counts = (
        f"{tot.unpriced} unpriced · {tot.skewed} skewed · {_locked_count(r)} locked · "
        f"{tot.nonmarketable} not marketable"
    )
    line2 = f"{ctx.cur} · {stamp} · {counts} · snapshot #{ctx.hash}"
    line3 = f"stashcard {__version__} · not affiliated with Valve or Steam · prices are informational, " "not offers"
    lines = [line1, line2]
    if r.fx_used:
        lines.append(f"FX {r.fx.label} for USD-only venues \u00b7 Steam = wallet funds, other venues = cash")
    lines.append(line3)
    yy = y + 22
    for line in lines:
        c.text((x0, yy), line, "mono", 11.5, col, max_w=x1 - x0)
        yy += 18
    return yy - 4


def venue_strip(c: Canvas, ctx: Ctx, y: float, *, x0: float = M, x1: float = W - M, title: bool = True) -> float:
    """'By marketplace': one tile per venue with its floor total, net after fee and coverage."""
    t, r = ctx.theme, ctx.report
    vts = [v for v in r.venue_totals if v.stacks]
    if len(vts) < 2:
        return y
    if title:
        c.label((x0, y), "by marketplace", 12, t.dim)
        c.label((x1, y), "at floor \u00b7 net after fee \u00b7 coverage", 11, t.faint if t.dark else t.dim, anchor="rs")
        y += 14
    n = len(vts)
    gap = 12
    tw = (x1 - x0 - (n - 1) * gap) / n
    best_cash = max((v for v in vts if not v.wallet), key=lambda v: v.net, default=None)
    for i, v in enumerate(vts):
        bx = x0 + i * (tw + gap)
        box = (bx, y, bx + tw, y + 86)
        c.panel(box, 14, shadow=False)
        if v is best_cash:
            c.rrect(box, 14, outline=t.good, width=1.5)
        tag_col = t.warn if v.wallet else t.good
        c.text((bx + 14, y + 24), v.name, "sans_semi", 14, t.text, max_w=tw - 70)
        c.pill(
            bx + tw - 10,
            y + 19,
            "WALLET" if v.wallet else "CASH",
            color=tag_col,
            size=9,
            role="mono_bold",
            pad_x=6,
            h=16,
            anchor="r",
        )
        val = ctx.total(v.value)
        size = fit_size(c, val, "mono_bold", 19, tw - 28, min_size=12)
        c.text((bx + 14, y + 52), val, "mono_bold", size, t.text)
        sub = f"net {ctx.total(v.net, compact=True)} \u00b7 {v.coverage(r.totals.lowest):.0%}"
        c.text((bx + 14, y + 72), sub, "mono", 11, t.good if v is best_cash else t.dim, max_w=tw - 28)
    return y + 86


def _locked_count(r: Report) -> int:
    return sum(1 for p in r.items if p.item.locked_qty)


def delta_line(c: Canvas, ctx: Ctx, x: float, y: float, max_w: float) -> float:
    t, r = ctx.theme, ctx.report
    d = r.delta
    if d is None:
        return y
    color = t.good if d.lowest_change >= 0 else t.bad
    c.label((x, y), f"since {d.since} · {d.when}", 12, t.dim)
    lx = x + c.measure(f"SINCE {d.since} · {d.when.upper()}", "mono_med", 12, tracking=0.96) + 18
    pct = f"{d.lowest_pct:+.1f}%" if d.lowest_pct is not None else ""
    if ctx.redaction.bands:
        main = f"{pct} at LOWEST"
    else:
        main = f"{ctx.money(d.lowest_change, sign=True)} ({pct}) at LOWEST"
    lx += c.text((lx, y), main, "mono_bold", 13, color) + 16
    if d.movers and lx < x + max_w - 120:
        m = d.movers[0]
        mv = f"top mover: {short_name(m.name)} {ctx.money(m.change, sign=True)}"
        c.text((lx, y), mv, "mono", 12, t.dim, max_w=x + max_w - lx)
    return y


def days_label(p: PricedItem, capture: float) -> str:
    d = p.days_to_sell(capture)
    if d is None:
        return "?"
    if d == math.inf:
        return "no sales"
    return days_text(d)
