"""Card layouts: summary (report card), game, grid, receipt, diff (then/now and A vs B), banner."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import timezone

from PIL import Image

from stashcard import __version__
from stashcard.diff import Diff
from stashcard.games import Game
from stashcard.inventory import display_name, short_name
from stashcard.money import fee_breakdown
from stashcard.redact import Redaction
from stashcard.render.canvas import Canvas
from stashcard.render.components import (
    FLAG_TEXT,
    Ctx,
    M,
    W,
    days_label,
    delta_line,
    fit_size,
    flag_color,
    header,
    icon_tile,
    item_row,
    profile_block,
    sorted_flags,
    stat_panel,
    trust_footer,
    venue_strip,
    warning_chips,
)
from stashcard.render.icons import accent_of, avatar_image
from stashcard.render.theme import Theme, mix
from stashcard.report import Report

MODES = ("summary", "game", "grid", "receipt", "diff")


# --------------------------------------------------------------------------------------
# Summary: the report card
# --------------------------------------------------------------------------------------
def render_summary(ctx: Ctx) -> Image.Image:
    r, t = ctx.report, ctx.theme
    tot = r.totals
    H = 1100
    c = Canvas(W, H, t)
    if t.glow:
        c.glow((W - 180, 120), 360, t.accent, 34)
        c.glow((120, 760), 380, t.accent2, 18)
    y = header(c, ctx, "report card")

    # Profile + liquidity grade
    py = y + 30
    profile_block(c, ctx, M, py, avatar=112, max_w=640)
    gb = (W - M - 270, py, W - M, py + 112)
    c.panel(gb, 18)
    share = r.sellable_share
    grade = r.liquidity_grade
    gcol = {"A": t.good, "B": t.good, "C": t.warn, "D": t.warn}.get(grade, t.bad if grade == "F" else t.dim)
    c.circle(gb[0] + 56, gb[1] + 56, 34, outline=gcol, width=2.5)
    c.text((gb[0] + 56, gb[1] + 57), grade, "mono_xbold", 36, gcol, anchor="mm")
    c.label((gb[0] + 106, gb[1] + 34), "sellable \u2264 7d", 11, t.dim)
    c.text((gb[0] + 106, gb[1] + 70), f"{share:.0%}" if share is not None else "n/a", "mono_xbold", 30, t.text)
    c.text(
        (gb[0] + 106, gb[1] + 92),
        f"of value, {r.opts.capture:.0%} capture",
        "mono",
        11,
        t.dim,
        max_w=gb[2] - gb[0] - 120,
    )

    # Hero numbers
    hy = py + 112 + 30
    gap = 16
    pw = (W - 2 * M - 2 * gap) / 3
    boxes = [(M + i * (pw + gap), hy, M + i * (pw + gap) + pw, hy + 156) for i in range(3)]
    median_total = sum(p.avg.median * p.qty for p in r.items if p.avg is not None and p.avg.median)
    lo_note = f"{tot.stacks} stacks · {tot.quantity:,} items · {tot.unpriced} unpriced"
    stat_panel(
        c, ctx, boxes[0], "LOWEST · floor now", ctx.total(tot.lowest), r.primary_lowest, lo_note, accent=t.accent
    )
    avg_note = f"median {ctx.total(median_total)} · {tot.skewed} skewed" if median_total else f"{tot.skewed} skewed"
    stat_panel(
        c, ctx, boxes[1], f"AVG · sales {r.opts.window}", ctx.total(tot.avg), r.primary_avg, avg_note, accent=t.accent2
    )
    cash_note = (
        f"cash on Skinport {ctx.total(tot.net_cash)} ({r.opts.skinport_fee:.0%} fee)"
        if tot.net_cash
        else "no Skinport floor for a cash estimate"
    )
    stat_panel(
        c,
        ctx,
        boxes[2],
        "NET ON STEAM · wallet",
        ctx.total(tot.net),
        "after Valve 5% + game 10% fees",
        cash_note,
        accent=t.palette[3],
    )

    # Columns: by game | top items
    cy = hy + 156 + 34
    left_x1 = M + 500
    c.label((M, cy), "value by game", 12, t.dim)
    c.label((left_x1, cy), "at LOWEST", 11, t.faint if t.dark else t.dim, anchor="rs")
    games = sorted([g for g in r.games if g.game is not None], key=lambda g: g.lowest, reverse=True)
    total = sum(g.lowest for g in games) or 1
    by = cy + 16
    bx = M
    bar_w = left_x1 - M
    c.rrect((M, by, left_x1, by + 12), 6, fill=mix(t.border, t.panel, 0.3))
    for g in games:
        if g.lowest <= 0:
            continue
        seg = bar_w * g.lowest / total
        c.rrect((bx, by, bx + max(seg, 3), by + 12), 6 if seg > 12 else 2, fill=t.game_color(g.game.slot))
        bx += seg
    ry = by + 44
    val_w = max([c.measure(ctx.total(g.lowest), "mono_bold", 15) for g in games[:5]] + [60])
    pct_x = left_x1 - val_w - 18
    for g in games[:5]:
        col = t.game_color(g.game.slot)
        c.rrect((M, ry - 11, M + 10, ry - 1), 2.5, fill=col)
        c.text((M + 20, ry), g.game.short, "sans_semi", 16, t.text, max_w=84)
        if g.error:
            c.text((M + 110, ry), "skipped: " + g.error, "mono", 11.5, t.bad, max_w=left_x1 - M - 110)
        else:
            c.text((M + 110, ry), f"{g.quantity:,} items", "mono", 12.5, t.dim, max_w=pct_x - M - 160)
            share_g = g.lowest / total if r.totals.lowest else 0
            c.text(
                (pct_x, ry),
                (f"{share_g:.0%}" if share_g >= 0.01 or not g.lowest else "<1%"),
                "mono",
                12.5,
                t.dim,
                anchor="rs",
            )
            c.text((left_x1, ry), ctx.total(g.lowest), "mono_bold", 15, t.text, anchor="rs")
        ry += 34
    if tot.nonmarketable:
        c.text(
            (M + 20, ry),
            f"+ {tot.nonmarketable} not-marketable stacks ({tot.nonmarketable_qty:,} items), " "not valued",
            "mono",
            11.5,
            t.faint if t.dark else t.dim,
            max_w=left_x1 - M - 20,
        )

    rx = left_x1 + 48
    rw = W - M - rx
    c.label((rx, cy), "top items", 12, t.dim)
    c.label((W - M, cy), "total at LOWEST", 11, t.faint if t.dark else t.dim, anchor="rs")
    iy = cy + 18
    for p in r.top(5):
        item_row(c, ctx, p, rx, iy, rw, icon=42)
        iy += 54
    if not r.items:
        games_txt = ", ".join(g.game.short for g in games) or "the selected games"
        c.text((rx, iy + 24), f"Nothing marketable in {games_txt}.", "sans_med", 15, t.dim, max_w=rw)
        iy += 40

    # By marketplace, warnings, delta, footer
    wy = max(ry, iy) + 20
    if sum(1 for v in r.venue_totals if v.stacks) > 1:
        wy = venue_strip(c, ctx, wy + 14) + 28
    c.line(M, wy - 8, W - M, wy - 8, t.rule, 1)
    warning_chips(c, ctx, M, wy + 16, W - M)
    fy = wy + 40
    if r.delta is not None:
        delta_line(c, ctx, M, fy + 12, W - 2 * M)
        fy += 28
    fy = trust_footer(c, ctx, fy + 6)
    return _crop(c, fy + 22)


def _crop(c: Canvas, height: float) -> Image.Image:
    """Layouts allocate generously, then cut the canvas at the real content end."""
    img = c.finish()
    return img.crop((0, 0, img.width, int(round(min(height, c.h)))))


# --------------------------------------------------------------------------------------
# Game card
# --------------------------------------------------------------------------------------
def render_game(ctx: Ctx, game: Game) -> Image.Image:
    r = ctx.report.subset(game)
    ctx = Ctx(r, ctx.theme, ctx.backend, ctx.redaction, ctx._icons)
    t = ctx.theme
    tot = r.totals
    rows = r.top(10)
    H = 520 + max(1, len(rows)) * 62 + 240
    c = Canvas(W, H, t)
    gcol = t.game_color(game.slot)
    if t.glow:
        c.glow((W - 200, 140), 380, gcol, 40)
    y = header(c, ctx, "game card")

    # Title row
    ty = y + 30
    c.text((M, ty + 44), game.name, "display", 46, t.text, max_w=W - 2 * M - 300)
    chips_y = ty + 76
    cx = M
    cx += c.pill(cx, chips_y, f"APP {game.appid}/{game.contextid}", color=gcol, size=11) + 8
    cx += c.pill(cx, chips_y, f"{tot.quantity:,} ITEMS · {tot.stacks} STACKS", color=t.dim, size=11) + 8
    if tot.nonmarketable:
        c.pill(cx, chips_y, f"{tot.nonmarketable} NOT MARKETABLE", color=t.dim, size=11)
    # mini profile on the right
    av = avatar_image(ctx.backend, r.profile, t, ctx.redaction.level, 128)
    ax = W - M - 52
    c.paste(av, (ax, ty + 8, ax + 52, ty + 60), radius=12)
    c.text((ax - 14, ty + 30), ctx.redaction.name(r.profile), "sans_semi", 17, t.text, anchor="rs", max_w=230)
    c.text((ax - 14, ty + 50), f"{game.short} inventory", "mono", 12, t.dim, anchor="rs")

    # Hero panels
    hy = ty + 108
    gap = 14
    pw = (W - 2 * M - 3 * gap) / 4
    bx = [(M + i * (pw + gap), hy, M + i * (pw + gap) + pw, hy + 132) for i in range(4)]
    stat_panel(c, ctx, bx[0], "LOWEST", ctx.total(tot.lowest), r.primary_lowest, accent=t.accent, value_size=32)
    stat_panel(
        c, ctx, bx[1], f"AVG {r.opts.window}", ctx.total(tot.avg), r.primary_avg, accent=t.accent2, value_size=32
    )
    stat_panel(
        c, ctx, bx[2], "NET STEAM (WALLET)", ctx.total(tot.net), "after Steam fees", accent=t.palette[3], value_size=32
    )
    stat_panel(
        c,
        ctx,
        bx[3],
        "NET SKINPORT (CASH)",
        ctx.total(tot.net_cash) if tot.net_cash else "n/a",
        f"after {r.opts.skinport_fee:.0%} fee" if game.skinport else "no Skinport market",
        accent=t.palette[2],
        value_size=32,
    )

    # Rarity distribution
    ry = hy + 132 + 40
    c.label((M, ry), "value by rarity", 12, t.dim)
    groups: dict[str, list] = defaultdict(lambda: [0, None, 0])
    for p in r.items:
        key = p.item.rarity_label or "Other"
        g = groups[key]
        g[0] += p.total_lowest or p.total_avg or 0
        g[1] = g[1] or accent_of(p.item, t)
        g[2] += p.qty
    order = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)
    total = sum(v[0] for _, v in order) or 1
    by = ry + 14
    c.rrect((M, by, W - M, by + 14), 7, fill=mix(t.border, t.panel, 0.3))
    bx0 = M
    for _name, (val, col, _qty) in order:
        seg = (W - 2 * M) * val / total
        if seg <= 0:
            continue
        c.rrect((bx0, by, bx0 + max(seg - 2, 3), by + 14), 7 if seg > 14 else 2, fill=col)
        bx0 += seg
    lx, ly = M, by + 38
    for name, (val, col, _qty) in order[:8]:
        share_r = val / total
        txt = f"{name} {share_r:.0%}" if share_r >= 0.01 or not val else f"{name} <1%"
        need = c.measure(txt, "mono", 12) + 30
        if lx + need > W - M:
            lx, ly = M, ly + 22
        c.rrect((lx, ly - 10, lx + 10, ly), 2.5, fill=col)
        c.text((lx + 16, ly), txt, "mono", 12, t.dim)
        lx += need
    # Item table
    ty2 = ly + 36
    cols = {"qty": W - M - 470, "lowest": W - M - 330, "avg": W - M - 170, "days": W - M}
    c.label((M, ty2), "item", 11, t.dim)
    c.label((cols["qty"], ty2), "qty", 11, t.dim, anchor="rs")
    c.label((cols["lowest"], ty2), "lowest (unit)", 11, t.dim, anchor="rs")
    c.label((cols["avg"], ty2), f"avg {r.opts.window} · median", 11, t.dim, anchor="rs")
    c.label((cols["days"], ty2), "days to sell", 11, t.dim, anchor="rs")
    yy = ty2 + 14
    if not rows:
        c.text((M, yy + 40), "No marketable items in this inventory.", "sans_med", 16, t.dim)
        yy += 70
    for p in rows:
        c.line(M, yy, W - M, yy, mix(t.rule, t.panel, 0.3), 1)
        icon_tile(c, ctx, p, (M, yy + 9, M + 44, yy + 53), radius=9)
        nx = M + 58
        role = "mono_med" if t.mono_names else "sans_semi"
        c.text(
            (nx, yy + 28), short_name(display_name(p.item)), role, 15, t.text, max_w=cols["qty"] - nx - 60, middle=True
        )
        meta = " · ".join(
            x
            for x in (
                p.item.rarity_label,
                p.item.exterior if not p.item.rarity else None,
                p.item.type if not p.item.rarity_label else None,
            )
            if x
        )
        mx = nx + c.text((nx, yy + 46), meta or p.item.type, "mono", 11, t.dim, max_w=280)
        for f in sorted_flags(p)[:3]:
            if f.startswith("fallback"):
                continue
            need = c.measure(FLAG_TEXT[f], "mono_bold", 9.5, tracking=0.38) + 12
            if mx + 8 + need > cols["qty"] - 50:
                break
            mx += 8 + c.pill(
                mx + 8, yy + 42, FLAG_TEXT[f], color=flag_color(t, f), size=9.5, role="mono_bold", pad_x=6, h=16
            )
        c.text((cols["qty"], yy + 28), f"{p.qty:,}", "mono_bold", 14, t.text, anchor="rs")
        lo = p.lowest
        c.text(
            (cols["lowest"], yy + 28),
            ctx.money(lo.amount) if lo else "n/a",
            "mono_bold",
            14,
            t.text if lo else t.faint,
            anchor="rs",
        )
        c.text(
            (cols["lowest"], yy + 46),
            lo.short if lo else "",
            "mono",
            10.5,
            t.warn if lo and lo.fallback else t.dim,
            anchor="rs",
            max_w=150,
        )
        av_ = p.avg
        c.text(
            (cols["avg"], yy + 28),
            ctx.money(av_.amount) if av_ else "n/a",
            "mono_bold",
            14,
            t.text if av_ else t.faint,
            anchor="rs",
        )
        sub = ""
        if av_:
            sub = f"med {ctx.money(av_.median)}" if av_.median else av_.short
        c.text(
            (cols["avg"], yy + 46),
            sub,
            "mono",
            10.5,
            t.warn if av_ and (av_.fallback or av_.skewed) else t.dim,
            anchor="rs",
            max_w=150,
        )
        c.text((cols["days"], yy + 28), days_label(p, r.opts.capture), "mono_bold", 14, t.text, anchor="rs")
        c.text(
            (cols["days"], yy + 46),
            "locked"
            if p.item.locked_qty
            else (f"{p.daily_volume:,.1f}/day" if p.daily_volume is not None else "volume n/a"),
            "mono",
            10.5,
            t.warn if p.item.locked_qty else t.dim,
            anchor="rs",
        )
        yy += 62
    c.line(M, yy, W - M, yy, mix(t.rule, t.panel, 0.3), 1)
    more = len(r.items) - len(rows)
    if more > 0:
        rest = sum(p.total_lowest or 0 for p in r.items[len(rows) :])
        c.text((M, yy + 24), f"+ {more} more stacks worth {ctx.total(rest)} at LOWEST", "mono", 12.5, t.dim)
        yy += 30
    wy = yy + 30
    warning_chips(c, ctx, M, wy, W - M)
    fy = trust_footer(c, ctx, wy + 26)
    return _crop(c, fy + 22)


# --------------------------------------------------------------------------------------
# Grid: the inventory picture
# --------------------------------------------------------------------------------------
GRID_COLS = 6


def grid_pages(report: Report, limit: int = 36, paginate: bool = False) -> list[list]:
    items = report.items
    if not paginate:
        return [items[:limit]]
    return [items[i : i + limit] for i in range(0, len(items), limit)] or [[]]


def render_grid(ctx: Ctx, page_items: list, page: int = 1, pages: int = 1, limit: int = 36) -> Image.Image:
    r, t = ctx.report, ctx.theme
    tot = r.totals
    gap = 14
    tile_w = (W - 2 * M - (GRID_COLS - 1) * gap) / GRID_COLS
    tile_h = tile_w + 78
    shown = list(page_items)
    remaining = []
    if pages == 1 and len(r.items) > len(shown):
        remaining = r.items[len(shown) :]
        if len(shown) >= limit:
            shown = shown[: limit - 1]
            remaining = r.items[len(shown) :]
    n_tiles = len(shown) + (1 if remaining else 0)
    rows = max(1, math.ceil(n_tiles / GRID_COLS))
    top = 214
    H = int(top + rows * (tile_h + gap) + 200)
    c = Canvas(W, H, t)
    if t.glow:
        c.glow((W / 2, 110), 520, t.accent, 22)
    y = header(c, ctx, "inventory")
    # title strip
    name = ctx.redaction.name(r.profile)
    av = avatar_image(ctx.backend, r.profile, t, ctx.redaction.level, 128)
    c.paste(av, (M, y + 26, M + 64, y + 90), radius=14)
    c.text((M + 80, y + 56), name, "display", 32, t.text, max_w=520)
    games = " · ".join(g.game.short for g in r.games if g.game is not None)
    sub = f"{tot.quantity:,} items · {tot.stacks} stacks · {games}"
    if pages > 1:
        sub += f" · page {page}/{pages}"
    c.text((M + 80, y + 82), sub, "mono", 13, t.dim, max_w=520)
    # totals on the right
    rx = W - M
    c.label((rx, y + 40), "total at lowest", 11, t.dim, anchor="rs")
    lo_txt = ctx.total(tot.lowest)
    size = fit_size(c, lo_txt, "mono_xbold", 34, 380)
    c.text((rx, y + 78), lo_txt, "mono_xbold", size, t.text, anchor="rs")
    c.text(
        (rx, y + 100),
        f"avg {r.opts.window} {ctx.total(tot.avg)} · net {ctx.total(tot.net)}",
        "mono",
        13,
        t.dim,
        anchor="rs",
        max_w=420,
    )

    gy = top
    for i, p in enumerate(shown):
        col, row = i % GRID_COLS, i // GRID_COLS
        x0 = M + col * (tile_w + gap)
        y0 = gy + row * (tile_h + gap)
        _grid_tile(c, ctx, p, (x0, y0, x0 + tile_w, y0 + tile_h))
    if remaining:
        i = len(shown)
        col, row = i % GRID_COLS, i // GRID_COLS
        x0 = M + col * (tile_w + gap)
        y0 = gy + row * (tile_h + gap)
        b = (x0, y0, x0 + tile_w, y0 + tile_h)
        c.panel(b, 14, shadow=False)
        c.dashed(x0 + 14, y0 + tile_h / 2, x0 + tile_w - 14, t.rule)
        val = sum(p.total_lowest or 0 for p in remaining)
        c.text((x0 + tile_w / 2, y0 + tile_h / 2 - 22), f"+{len(remaining)}", "mono_xbold", 34, t.text, anchor="ms")
        c.text((x0 + tile_w / 2, y0 + tile_h / 2 - 4), "more stacks", "mono", 12, t.dim, anchor="ms")
        c.text(
            (x0 + tile_w / 2, y0 + tile_h / 2 + 28),
            ctx.total(val),
            "mono_bold",
            15,
            t.text,
            anchor="ms",
            max_w=tile_w - 20,
        )
        c.text((x0 + tile_w / 2, y0 + tile_h / 2 + 46), "at LOWEST", "mono", 11, t.dim, anchor="ms")
    end = gy + rows * (tile_h + gap)
    warning_chips(c, ctx, M, end + 18, W - M)
    fy = trust_footer(c, ctx, end + 44)
    return _crop(c, fy + 22)


def _grid_tile(c: Canvas, ctx: Ctx, p, b) -> None:
    t = ctx.theme
    x0, y0, x1, y1 = b
    accent = accent_of(p.item, t)
    c.panel(b, 14, shadow=t.dark, border=True)
    art = (x0 + 8, y0 + 8, x1 - 8, y0 + (x1 - x0) - 8)
    c.vgradient(art, mix(accent, t.panel, 0.74 if t.dark else 0.84), mix(accent, t.panel, 0.93), radius=10)
    if t.glow:
        c.glow(((art[0] + art[2]) / 2, (art[1] + art[3]) / 2), (art[2] - art[0]) * 0.55, accent, 46)
    im, real = ctx.icon(p, 256)
    c.paste_contain(im, art, pad=8 if real else 0)
    # rarity strip + text label (colour is never the only signal)
    c.rrect((art[0], art[3] - 4, art[2], art[3]), 2, fill=accent)
    rl = p.item.rarity_label
    if rl:
        c.text(
            (art[0] + 8, art[1] + 16),
            rl.upper(),
            "mono_bold",
            9,
            mix(accent, t.text, 0.35),
            tracking=0.5,
            max_w=art[2] - art[0] - 50,
        )
    if p.qty > 1:
        q = f"x{p.qty:,}"
        qw = c.measure(q, "mono_bold", 11) + 12
        c.rrect(
            (art[2] - qw - 6, art[1] + 6, art[2] - 6, art[1] + 24),
            9,
            fill=(0, 0, 0, 150) if t.dark else (255, 255, 255, 210),
        )
        c.text((art[2] - 6 - qw / 2, art[1] + 15), q, "mono_bold", 11, t.text, anchor="mm")
    flags = [f for f in sorted_flags(p) if not f.startswith("fallback")]
    if flags:
        f = flags[0]
        c.pill(
            art[0] + 6,
            art[3] - 16,
            FLAG_TEXT[f],
            color=flag_color(t, f),
            size=8.5,
            role="mono_bold",
            pad_x=5,
            h=15,
            fill=(0, 0, 0, 140) if t.dark else (255, 255, 255, 220),
            outline=False,
        )
    name = short_name(display_name(p.item))
    lines = c.wrap(name, "mono_med" if t.mono_names else "sans_semi", 12.5, x1 - x0 - 20, max_lines=2)
    ny = art[3] + 20
    for ln in lines:
        c.text((x0 + 10, ny), ln, "mono_med" if t.mono_names else "sans_semi", 12.5, t.text)
        ny += 16
    price = (
        ctx.money(p.total_lowest)
        if p.total_lowest is not None
        else (ctx.money(p.total_avg) if p.total_avg is not None else "unpriced")
    )
    c.text((x0 + 10, y1 - 12), price, "mono_bold", 14, t.text if p.priced else t.bad, max_w=x1 - x0 - 20)
    if p.qty > 1 and p.lowest is not None:
        c.text((x1 - 10, y1 - 12), f"{ctx.money(p.lowest.amount, compact=True)} ea", "mono", 10.5, t.dim, anchor="rs")


# --------------------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------------------
RECEIPT_PAPER = 560
RECEIPT_SIDE = 80
RECEIPT_TOP = 64


def render_receipt(ctx: Ctx, max_rows: int = 26) -> Image.Image:
    """Thermal-receipt card. Laid out twice: once to measure, once to draw on paper cut to size."""
    t = ctx.theme
    width = RECEIPT_PAPER + 2 * RECEIPT_SIDE
    probe = Canvas(width, 4000, t, ss=1, background=False)
    end = _receipt_body(probe, ctx, max_rows)
    paper_h = end - RECEIPT_TOP + 26
    H = int(RECEIPT_TOP + paper_h + 64)
    c = Canvas(width, H, t)
    if t.glow:
        c.glow((width / 2, 160), 320, t.accent, 26)
    px0 = RECEIPT_SIDE
    paper = Canvas(RECEIPT_PAPER, int(paper_h) + 12, t, ss=c.ss, background=False)
    paper.rrect((0, 0, RECEIPT_PAPER, paper_h + 12), 0, fill=t.paper)
    paper.grain(0.7)
    _teeth(paper, top=True)
    _teeth(paper, top=False, y=paper_h + 12)
    c.shadow(
        (px0, RECEIPT_TOP + 6, px0 + RECEIPT_PAPER, RECEIPT_TOP + paper_h + 6),
        4,
        blur=22,
        alpha=130 if t.dark else 50,
        dy=10,
    )
    c.img.alpha_composite(paper.img, (c.s(px0), c.s(RECEIPT_TOP)))
    _receipt_body(c, ctx, max_rows)
    return c.finish()


def _teeth(c: Canvas, top: bool, y: float = 0, tooth: float = 10) -> None:
    """Punch a zigzag torn edge into the paper (transparent triangles)."""
    from PIL import ImageDraw

    d = ImageDraw.Draw(c.img)
    x = 0.0
    depth = tooth * 0.75
    while x < c.w:
        if top:
            pts = [(c.s(x), 0), (c.s(x + tooth), 0), (c.s(x + tooth / 2), c.s(depth))]
        else:
            pts = [(c.s(x), c.s(y)), (c.s(x + tooth), c.s(y)), (c.s(x + tooth / 2), c.s(y - depth))]
        d.polygon(pts, fill=(0, 0, 0, 0))
        x += tooth


def _receipt_body(c: Canvas, ctx: Ctx, max_rows: int) -> float:
    r, t = ctx.report, ctx.theme
    tot = r.totals
    ink, faint = t.ink, mix(t.ink, t.paper, 0.48)
    x0, x1 = RECEIPT_SIDE + 34, RECEIPT_SIDE + RECEIPT_PAPER - 34
    line_h = 22
    y = RECEIPT_TOP + 56
    rows = r.items[:max_rows]

    def center(text, role="mono_bold", size=15.0, color=ink, gap=line_h):
        nonlocal y
        c.text(((x0 + x1) / 2, y), text, role, size, color, anchor="ms", max_w=x1 - x0)
        y += gap

    def lr(left, right, role="mono", size=13.5, color=ink, leader=True):
        nonlocal y
        rw = c.measure(right, role, size)
        lw = c.text((x0, y), left, role, size, color, max_w=x1 - x0 - rw - 24)
        if leader:
            c.dotted(x0 + lw + 8, y - 4, x1 - rw - 8, faint, step=5.2, r=0.8)
        c.text((x1, y), right, role, size, color, anchor="rs")
        y += line_h

    def rule(double=False):
        nonlocal y
        if double:
            c.line(x0, y - 10, x1, y - 10, ink, 1)
            c.line(x0, y - 6, x1, y - 6, ink, 1)
        else:
            c.dashed(x0, y - 8, x1, faint, dash=5, gap=4)
        y += line_h * 0.85

    center("* STASHCARD *", "mono_xbold", 24, gap=26)
    center("INVENTORY VALUATION RECEIPT", "mono_med", 12.5)
    url = ctx.redaction.url(r.profile)
    center(url or f"PROFILE {ctx.redaction.steamid(r.profile)}", "mono", 12, faint, gap=19)
    stamp = r.generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    center(f"{stamp}   #{ctx.hash}", "mono", 12, faint, gap=19)
    if r.is_demo:
        center("DEMO DATA \u00b7 FICTIONAL PROFILE", "mono_bold", 12, ink, gap=19)
    y += 8
    rule()
    c.text((x0, y), "QTY  ITEM", "mono_bold", 12.5, ink)
    c.text((x1, y), "LOWEST", "mono_bold", 12.5, ink, anchor="rs")
    y += line_h
    qty_w = c.measure("0000 ", "mono", 13.5)
    for p in rows:
        price = (
            ctx.money(p.total_lowest)
            if p.total_lowest is not None
            else (ctx.money(p.total_avg) + "*" if p.total_avg is not None else "N/A")
        )
        name = short_name(display_name(p.item)).upper()
        rw = c.measure(price, "mono", 13.5)
        c.text((x0 + qty_w - 10, y), f"{p.qty:,}", "mono", 13.5, ink, anchor="rs")
        nx = x0 + qty_w
        lw = c.text((nx, y), name, "mono", 13.5, ink, max_w=x1 - nx - rw - 30, middle=True)
        c.dotted(nx + lw + 8, y - 4, x1 - rw - 8, faint, step=5.2, r=0.8)
        c.text((x1, y), price, "mono", 13.5, ink, anchor="rs")
        y += line_h
    if len(r.items) > max_rows:
        rest = r.items[max_rows:]
        lr(f"... {len(rest)} MORE STACKS", ctx.money(sum(p.total_lowest or 0 for p in rest)), color=faint)
    rule()
    for g in sorted([g for g in r.games if g.game is not None], key=lambda g: g.lowest, reverse=True):
        lr(
            f"SUBTOTAL {g.game.short.upper()}",
            "SKIPPED" if g.error else ctx.total(g.lowest),
            color=faint if g.error else ink,
        )
    rule(double=True)
    lr("TOTAL @ LOWEST", ctx.total(tot.lowest), "mono_xbold", 16, leader=False)
    lr(f"TOTAL @ AVG {r.opts.window.upper()}", ctx.total(tot.avg), "mono_bold", 13.5, leader=False)
    rule()
    floors = [p for p in r.items if p.steam_floor]
    gross = sum(p.steam_floor * p.qty for p in floors)
    steam_fee = sum(fee_breakdown(p.steam_floor)["steam_fee"] * p.qty for p in floors)
    pub_fee = sum(fee_breakdown(p.steam_floor)["publisher_fee"] * p.qty for p in floors)
    lr("GROSS @ STEAM FLOOR", ctx.total(gross))
    if not ctx.redaction.bands:
        lr("  STEAM FEE (5%)", ctx.money(-steam_fee), color=faint)
        lr("  GAME FEE (10%)", ctx.money(-pub_fee), color=faint)
    lr("NET ON STEAM (WALLET)", ctx.total(tot.net), "mono_bold", 13.5)
    if tot.net_cash:
        lr(f"NET ON SKINPORT (CASH {r.opts.fee_for('skinport'):.0%})", ctx.total(tot.net_cash), "mono_bold", 13.5)
    vts = [v for v in r.venue_totals if v.stacks]
    if len(vts) > 1:
        rule()
        c.text((x0, y), "BY MARKETPLACE", "mono_bold", 12.5, ink)
        c.text((x1, y), "FLOOR / NET", "mono_bold", 12.5, ink, anchor="rs")
        y += line_h
        for v in vts:
            fee = "VALVE" if v.fee is None else f"{v.fee:.0%}"
            kind = "W" if v.wallet else "C"
            lr(
                f"{v.name.upper()} {kind} {fee} {v.coverage(tot.lowest):.0%}",
                f"{ctx.total(v.value, compact=True)} / {ctx.total(v.net, compact=True)}",
            )
        route, _missing = r.best_cash_route
        lr("BEST CASH ROUTE (NET)", ctx.total(route), "mono_bold", 13.5)
        c.text((x0, y), "W = STEAM WALLET FUNDS, C = CASH. % = FEE, COVERAGE", "mono", 11, faint, max_w=x1 - x0)
        y += 17
        if r.fx_used:
            c.text((x0, y), f"FX {r.fx.short}".upper(), "mono", 11, faint, max_w=x1 - x0)
            y += 17
    rule()
    locked = sum(1 for p in r.items if p.item.locked_qty)
    share = r.sellable_share
    lr("ITEMS / STACKS", f"{tot.quantity:,} / {tot.stacks}")
    lr("UNPRICED / SKEWED", f"{tot.unpriced} / {tot.skewed}")
    lr("LOCKED / NOT MARKETABLE", f"{locked} / {tot.nonmarketable}")
    lr("SELLABLE WITHIN 7 DAYS", f"{share:.0%}" if share is not None else "N/A")
    rule()
    for text in (f"LOWEST: {r.lowest_label}", f"AVG: {r.avg_label}"):
        for ln in c.wrap(text.upper(), "mono", 11.5, x1 - x0, max_lines=2):
            c.text((x0, y), ln, "mono", 11.5, faint)
            y += 17
    if any(p.lowest is None and p.avg is not None for p in rows):
        c.text((x0, y), "* NO FLOOR: VALUED AT AVG", "mono", 11.5, faint)
        y += 17
    y += 12
    _barcode(c, ctx.hash + r.profile.steamid64[-4:], (x0 + 30, y, x1 - 30, y + 48), ink)
    y += 70
    center("THANK YOU FOR HODLING", "mono_bold", 13.5)
    center("NO REFUNDS. PRICES ARE INFORMATIONAL.", "mono", 11.5, faint, gap=18)
    center("NOT AFFILIATED WITH VALVE OR STEAM.", "mono", 11.5, faint, gap=18)
    center(f"STASHCARD {__version__}", "mono", 11.5, faint, gap=18)
    return y


def _barcode(c: Canvas, seed: str, box, ink: str) -> None:
    x0, y0, x1, y1 = box
    bits = "".join(f"{b:08b}" for b in hashlib.sha256(seed.encode()).digest())
    x = x0
    i = 0
    while x < x1:
        wide = bits[i % len(bits)] == "1"
        bw = 2.6 if wide else 1.2
        if bits[(i + 7) % len(bits)] == "1":
            c.draw.rectangle((c.s(x), c.s(y0), c.s(x + bw) - 1, c.s(y1)), fill=c.col(ink))
        x += bw + 1.1
        i += 1


# --------------------------------------------------------------------------------------
# Diff: then vs now, or A vs B
# --------------------------------------------------------------------------------------
def render_diff(
    diff: Diff, theme: Theme, backend=None, red_old: Redaction | None = None, red_new: Redaction | None = None
) -> Image.Image:
    if diff.mode == "versus":
        return _render_versus(diff, theme, backend, red_old or Redaction("light"), red_new or Redaction("light"))
    return _render_timeline(diff, theme, backend, red_new or Redaction("none"))


def _change_row(c: Canvas, ctx: Ctx, ch, x: float, y: float, w: float, mode: str) -> None:
    t = ctx.theme
    icon_tile(c, ctx, ch.item, (x, y, x + 38, y + 38), radius=8)
    tx = x + 50
    if mode == "move":
        val = ctx.money(ch.change, sign=True)
        col = t.good if ch.change >= 0 else t.bad
        sub = f"{ctx.money(ch.value_old)} → {ctx.money(ch.value_new)}"
        if ch.qty_old != ch.qty_new:
            sub += f" · x{ch.qty_old}→x{ch.qty_new}"
        elif ch.pct is not None:
            sub += f" · {ch.pct:+.1f}%"
    elif mode == "added":
        val, col, sub = ctx.money(ch.value_new), t.good, f"x{ch.qty_new:,} added"
    else:
        val, col, sub = ctx.money(ch.value_old), t.faint if t.dark else t.dim, f"x{ch.qty_old:,} gone"
    vw = c.measure(val, "mono_bold", 14)
    c.text((x + w, y + 16), val, "mono_bold", 14, col, anchor="rs")
    c.text(
        (tx, y + 16),
        short_name(ch.name),
        "mono_med" if t.mono_names else "sans_semi",
        14,
        t.text,
        max_w=w - 50 - vw - 14,
        middle=True,
    )
    c.text((tx, y + 33), sub, "mono", 11, t.dim, max_w=w - 50)


def _render_timeline(diff: Diff, t: Theme, backend, red: Redaction) -> Image.Image:
    r = diff.new
    ctx = Ctx(r, t, backend, red)
    H = 1100
    c = Canvas(W, H, t)
    up = diff.lowest_change >= 0
    if t.glow:
        c.glow((W - 260, 180), 420, t.good if up else t.bad, 30)
    y = header(c, ctx, "then vs now")
    old_d = diff.old.generated_at.strftime("%Y-%m-%d")
    new_d = r.generated_at.strftime("%Y-%m-%d")
    ty = y + 32
    av = avatar_image(backend, r.profile, t, red.level, 128)
    c.paste(av, (M, ty, M + 56, ty + 56), radius=12)
    c.text((M + 72, ty + 24), red.name(r.profile), "sans_semi", 20, t.text, max_w=520)
    c.text((M + 72, ty + 48), f"{old_d}  →  {new_d}  ·  {diff.span_text}", "mono", 13.5, t.dim)
    # big change
    by = ty + 96
    col = t.good if up else t.bad
    pct = f"{diff.lowest_pct:+.1f}%" if diff.lowest_pct is not None else ""
    big = pct if red.bands else ctx.money(diff.lowest_change, sign=True)
    size = fit_size(c, big, "mono_xbold", 72, 620)
    c.label((M, by), "change at LOWEST", 12, t.dim)
    c.text((M, by + 76), big, "mono_xbold", size, col)
    if not red.bands:
        c.text((M + c.measure(big, "mono_xbold", size) + 18, by + 74), pct, "mono_bold", 24, col)
    # side panel: then/now totals
    sp = (W - M - 420, by - 14, W - M, by + 98)
    c.panel(sp, 16)
    c.label((sp[0] + 22, sp[1] + 30), "then", 11, t.dim)
    c.label((sp[0] + 150, sp[1] + 30), "now", 11, t.dim)
    c.label((sp[0] + 278, sp[1] + 30), "change", 11, t.dim)
    rows_ = [
        ("LOWEST", diff.old.totals.lowest, r.totals.lowest, diff.lowest_change),
        (f"AVG {r.opts.window}", diff.old.totals.avg, r.totals.avg, diff.avg_change),
    ]
    yy = sp[1] + 58
    for name, a, b, d in rows_:
        c.text((sp[0] + 22, yy), ctx.total(a, compact=True), "mono_bold", 15, t.text)
        c.text((sp[0] + 150, yy), ctx.total(b, compact=True), "mono_bold", 15, t.text)
        dtxt = (f"{d / a * 100:+.1f}%" if a else "") if red.bands else ctx.money(d, sign=True, compact=True)
        c.text((sp[0] + 278, yy), dtxt, "mono_bold", 15, t.good if d >= 0 else t.bad)
        c.text((sp[2] - 18, yy), name, "mono", 10.5, t.dim, anchor="rs")
        yy += 30
    # gainers / losers
    gy = by + 140
    colw = (W - 2 * M - 40) / 2
    c.label((M, gy), f"top gainers ({len(diff.gainers)})", 12, t.good)
    c.label((M + colw + 40, gy), f"top losers ({len(diff.losers)})", 12, t.bad)
    ry = gy + 16
    for i in range(5):
        if i < len(diff.gainers):
            _change_row(c, ctx, diff.gainers[i], M, ry + i * 50, colw, "move")
        if i < len(diff.losers):
            _change_row(c, ctx, diff.losers[i], M + colw + 40, ry + i * 50, colw, "move")
    if not diff.gainers:
        c.text((M, ry + 24), "Nothing went up.", "mono", 13, t.dim)
    if not diff.losers:
        c.text((M + colw + 40, ry + 24), "Nothing went down.", "mono", 13, t.dim)
    ay = ry + 5 * 50 + 26
    c.line(M, ay - 18, W - M, ay - 18, t.rule, 1)
    c.label((M, ay), f"added ({len(diff.added)})", 12, t.dim)
    c.label((M + colw + 40, ay), f"removed ({len(diff.removed)})", 12, t.dim)
    ry2 = ay + 16
    shown = 4
    for i in range(shown):
        if i < len(diff.added):
            _change_row(c, ctx, diff.added[i], M, ry2 + i * 50, colw, "added")
        if i < len(diff.removed):
            _change_row(c, ctx, diff.removed[i], M + colw + 40, ry2 + i * 50, colw, "removed")
    n_rows = max(1, min(shown, max(len(diff.added), len(diff.removed))))
    if not diff.added:
        c.text((M, ry2 + 24), "No new items.", "mono", 13, t.dim)
    if not diff.removed:
        c.text((M + colw + 40, ry2 + 24), "Nothing left the inventory.", "mono", 13, t.dim)
    end = ry2 + n_rows * 50 + 6
    if len(diff.added) > shown:
        c.text((M, end + 8), f"+ {len(diff.added) - shown} more added", "mono", 12, t.dim)
    if len(diff.removed) > shown:
        c.text((M + colw + 40, end + 8), f"+ {len(diff.removed) - shown} more removed", "mono", 12, t.dim)
    fy = end + 34
    c.text(
        (M, fy),
        f"then #{_h(diff.old)} · now #{_h(r)} · {diff.shared} items in both · values at " "LOWEST (AVG where no floor)",
        "mono",
        11.5,
        t.faint if t.dark else t.dim,
        max_w=W - 2 * M,
    )
    fy = trust_footer(c, ctx, fy + 16)
    return _crop(c, fy + 22)


def _h(r: Report) -> str:
    from stashcard.snapshot import snapshot_hash

    return snapshot_hash(r)


def _render_versus(diff: Diff, t: Theme, backend, red_a: Redaction, red_b: Redaction) -> Image.Image:
    a, b = diff.old, diff.new
    ctx_a, ctx_b = Ctx(a, t, backend, red_a), Ctx(b, t, backend, red_b)
    H = 1060
    c = Canvas(W, H, t)
    if t.glow:
        c.glow((300, 200), 360, t.accent, 24)
        c.glow((900, 200), 360, t.accent2, 24)
    y = header(c, ctx_b, "a vs b")
    colw = (W - 2 * M - 60) / 2
    xs = [M, M + colw + 60]
    vs_y = y + 30
    col_end = vs_y
    for x, ctx, side, accent in ((xs[0], ctx_a, "A", t.accent), (xs[1], ctx_b, "B", t.accent2)):
        r = ctx.report
        av = avatar_image(backend, r.profile, t, ctx.redaction.level, 128)
        c.paste(av, (x, vs_y, x + 64, vs_y + 64), radius=14)
        c.pill(x + 80, vs_y + 14, f"PROFILE {side}", color=accent, size=10.5)
        c.text((x + 80, vs_y + 52), ctx.redaction.name(r.profile), "display_bold", 26, t.text, max_w=colw - 90)
        py = vs_y + 96
        box = (x, py, x + colw, py + 128)
        c.panel(box, 16)
        c.rrect((x + 1, py + 18, x + 4, py + 46), 1.5, fill=accent)
        c.label((x + 24, py + 32), "lowest", 11, t.dim)
        v = ctx.total(r.totals.lowest)
        size = fit_size(c, v, "mono_xbold", 36, colw - 48)
        c.text((x + 24, py + 76), v, "mono_xbold", size, t.text)
        c.text(
            (x + 24, py + 104),
            f"avg {r.opts.window} {ctx.total(r.totals.avg)} · net {ctx.total(r.totals.net)}",
            "mono",
            12.5,
            t.dim,
            max_w=colw - 48,
        )
        # game bars
        gy = py + 160
        c.label((x, gy), "by game", 11, t.dim)
        tot = max(1, max(ctx_a.report.totals.lowest, ctx_b.report.totals.lowest))
        gy += 22
        shown_games = [g for g in r.games if g.game is not None and (g.stacks or g.error)]
        for g in sorted(shown_games, key=lambda g: g.lowest, reverse=True)[:5]:
            w = (colw - 170) * g.lowest / tot
            c.text((x, gy + 4), g.game.short, "sans_semi", 13.5, t.text)
            c.rrect((x + 70, gy - 6, x + 70 + colw - 170, gy + 4), 5, fill=mix(t.border, t.panel, 0.3))
            if w > 0:
                c.rrect((x + 70, gy - 6, x + 70 + max(w, 4), gy + 4), 5, fill=t.game_color(g.game.slot))
            c.text((x + colw, gy + 4), ctx.total(g.lowest, compact=True), "mono_bold", 13, t.text, anchor="rs")
            gy += 28
        # top items
        ty = py + 160 + 22 + 5 * 28 + 20
        c.label((x, ty), "top items", 11, t.dim)
        iy = ty + 14
        for p in r.top(5):
            item_row(c, ctx, p, x, iy, colw, icon=38, show_avg=False)
            iy += 48
        col_end = max(col_end, iy)
    # centre divider and verdict
    mid = W / 2
    c.line(mid, vs_y, mid, col_end - 10, t.rule, 1)
    c.circle(mid, vs_y + 32, 22, fill=t.panel, outline=t.border)
    c.text((mid, vs_y + 33), "VS", "mono_xbold", 14, t.text, anchor="mm")
    vy = col_end + 44
    diffv = b.totals.lowest - a.totals.lowest
    lead = "B" if diffv > 0 else "A"
    small = max(1, min(a.totals.lowest, b.totals.lowest))
    ratio = max(a.totals.lowest, b.totals.lowest) / small
    rel = f"{ratio:.1f}x" if ratio >= 2 else f"+{(ratio - 1) * 100:.0f}%"
    c.line(M, vy - 30, W - M, vy - 30, t.rule, 1)
    verdict = (
        f"PROFILE {lead} HOLDS "
        + (f"{rel} THE VALUE" if (red_a.bands or red_b.bands) else f"{ctx_b.money(abs(diffv))} MORE ({rel})")
        + " AT LOWEST"
    )
    if diffv == 0:
        verdict = "DEAD EVEN AT LOWEST"
    c.text((M, vy), verdict, "mono_xbold", 18, t.text, max_w=W - 2 * M)
    c.text(
        (M, vy + 26),
        f"{diff.shared} items in both · {len(diff.removed)} only in A · {len(diff.added)} " f"only in B · {a.currency}",
        "mono",
        12.5,
        t.dim,
    )
    if a.currency != b.currency:
        c.text((M, vy + 46), "Different currencies: totals are not comparable.", "mono_bold", 12.5, t.bad)
    fy = trust_footer(c, ctx_b, vy + 56)
    c.text(
        (M, fy + 6),
        f"A #{_h(a)} · B #{_h(b)} · sources and windows as labelled above, per profile",
        "mono",
        11.5,
        t.faint if t.dark else t.dim,
        max_w=W - 2 * M,
    )
    return _crop(c, fy + 30)


# --------------------------------------------------------------------------------------
# Markets: the same inventory at every marketplace
# --------------------------------------------------------------------------------------
def render_markets(ctx: Ctx, top: int = 8) -> Image.Image:
    from stashcard.pricing import venue_label

    r, t = ctx.report, ctx.theme
    tot = r.totals
    vts = r.venue_totals
    rows = r.top(top)
    H = 1500 + len(vts) * 56
    c = Canvas(W, H, t)
    if t.glow:
        c.glow((W - 220, 160), 400, t.accent, 30)
    y = header(c, ctx, "markets")
    ty = y + 30
    c.text((M, ty + 40), "Where your stash is worth the most", "display", 36, t.text, max_w=W - 2 * M - 300)
    sub = f"{tot.quantity:,} items \u00b7 {tot.stacks} stacks \u00b7 floors now, net after each seller fee"
    c.text((M, ty + 70), sub, "mono", 13, t.dim, max_w=W - 2 * M - 300)
    av = avatar_image(ctx.backend, r.profile, t, ctx.redaction.level, 128)
    ax = W - M - 52
    c.paste(av, (ax, ty + 8, ax + 52, ty + 60), radius=12)
    c.text((ax - 14, ty + 30), ctx.redaction.name(r.profile), "sans_semi", 17, t.text, anchor="rs", max_w=230)
    c.text((ax - 14, ty + 50), r.currency, "mono", 12, t.dim, anchor="rs")

    # Venue table
    py = ty + 100
    ph = 52 + 56 * max(1, len(vts))
    c.panel((M, py, W - M, py + ph), 18)
    cols = {
        "venue": M + 24,
        "money": M + 260,
        "cov": M + 360,
        "floor": W - M - 400,
        "fee": W - M - 300,
        "net": W - M - 140,
    }
    hy = py + 32
    c.label((cols["venue"], hy), "marketplace", 11, t.dim)
    c.label((cols["money"], hy), "money", 11, t.dim)
    c.label((cols["cov"], hy), "coverage of your value", 11, t.dim)
    c.label((cols["floor"], hy), "at floor", 11, t.dim, anchor="rs")
    c.label((cols["fee"], hy), "fee", 11, t.dim, anchor="rs")
    c.label((cols["net"], hy), "net after fee", 11, t.dim, anchor="rs")
    live = [v for v in vts if v.stacks]
    best_cash = max((v for v in live if not v.wallet), key=lambda v: v.net, default=None)
    ry = py + 52
    for v in vts:
        c.line(M + 20, ry, W - M - 20, ry, mix(t.rule, t.panel, 0.3), 1)
        yy = ry + 34
        c.text((cols["venue"], yy - 4), v.name, "sans_semi", 17, t.text, max_w=220)
        if v.stacks:
            c.text((cols["venue"], yy + 14), f"{v.stacks} stacks", "mono", 11, t.dim)
        c.pill(
            cols["money"],
            yy - 9,
            "WALLET" if v.wallet else "CASH",
            color=t.warn if v.wallet else t.good,
            size=10,
            role="mono_bold",
            pad_x=7,
            h=19,
        )
        bar_w = cols["floor"] - cols["cov"] - 190
        cov = v.coverage(tot.lowest)
        c.rrect((cols["cov"], yy - 14, cols["cov"] + bar_w, yy - 4), 5, fill=mix(t.border, t.panel, 0.3))
        if cov > 0:
            c.rrect((cols["cov"], yy - 14, cols["cov"] + max(6, bar_w * cov), yy - 4), 5, fill=t.palette[3])
        c.text((cols["cov"] + bar_w + 12, yy - 4), f"{cov:.0%}", "mono", 12.5, t.dim)
        if not v.stacks:
            c.text((cols["net"], yy - 4), "no quotes", "mono", 13, t.faint if t.dark else t.dim, anchor="rs")
        else:
            c.text((cols["floor"], yy - 4), ctx.total(v.value), "mono_bold", 15, t.text, anchor="rs")
            c.text((cols["fee"], yy - 4), "Valve" if v.fee is None else f"{v.fee:.0%}", "mono", 13, t.dim, anchor="rs")
            good = v is best_cash
            c.text((cols["net"], yy - 4), ctx.total(v.net), "mono_bold", 15, t.good if good else t.text, anchor="rs")
            if good:
                c.pill(
                    W - M - 24, yy - 9, "BEST CASH", color=t.good, size=9.5, role="mono_bold", pad_x=6, h=18, anchor="r"
                )
        ry += 56

    # Callouts
    cy = py + ph + 26
    gap = 16
    cw = (W - 2 * M - 2 * gap) / 3
    route, missing = r.best_cash_route
    steam_net = next((v.net for v in vts if v.venue == "steam" and v.stacks), None)
    cheapest = sum((p.best_buy().amount * p.qty) for p in r.items if p.best_buy())
    stat_panel(
        c,
        ctx,
        (M, cy, M + cw, cy + 140),
        "BEST CASH ROUTE",
        ctx.total(route),
        "each stack at its best cash venue",
        f"{missing} stack(s) without a cash quote",
        accent=t.good,
        value_size=32,
    )
    stat_panel(
        c,
        ctx,
        (M + cw + gap, cy, M + 2 * cw + gap, cy + 140),
        "STEAM NET \u00b7 WALLET",
        ctx.total(steam_net) if steam_net is not None else "n/a",
        "after Valve fees",
        "wallet funds cannot be withdrawn",
        accent=t.warn,
        value_size=32,
    )
    stat_panel(
        c,
        ctx,
        (M + 2 * (cw + gap), cy, W - M, cy + 140),
        "CHEAPEST TO REBUY",
        ctx.total(cheapest),
        "lowest floor per stack, any venue",
        "what the same stash costs today",
        accent=t.accent2,
        value_size=32,
    )

    # Top items x venues
    iy = cy + 140 + 40
    venue_cols = [v.venue for v in live]
    c.label((M, iy), "top items: floor at each venue", 12, t.dim)
    c.label((W - M, iy), "best cash sell (net)", 11, t.faint if t.dark else t.dim, anchor="rs")
    name_w = 300
    sell_w = 190
    span = W - 2 * M - name_w - sell_w
    vw = span / max(1, len(venue_cols))
    hy2 = iy + 30
    for j, key in enumerate(venue_cols):
        cx = M + name_w + vw * (j + 1) - 12
        c.text((cx, hy2), venue_label(key), "mono_med", 11.5, t.dim, anchor="rs", max_w=vw - 10)
    yy = hy2 + 12
    for p in rows:
        c.line(M, yy, W - M, yy, mix(t.rule, t.panel, 0.3), 1)
        icon_tile(c, ctx, p, (M, yy + 8, M + 40, yy + 48), radius=8)
        c.text(
            (M + 52, yy + 25),
            short_name(display_name(p.item)),
            "mono_med" if t.mono_names else "sans_semi",
            14,
            t.text,
            max_w=name_w - 60,
            middle=True,
        )
        c.text(
            (M + 52, yy + 42),
            f"x{p.qty:,}" + (f" \u00b7 {p.item.rarity_label}" if p.item.rarity_label else ""),
            "mono",
            11,
            t.dim,
            max_w=name_w - 60,
        )
        buy = p.best_buy()
        for j, key in enumerate(venue_cols):
            cx = M + name_w + vw * (j + 1) - 12
            q = p.venues.get(key)
            if q is None:
                c.text((cx, yy + 30), "-", "mono", 13, t.faint if t.dark else t.dim, anchor="rs")
                continue
            cheap = buy is not None and q.venue == buy.venue
            c.text(
                (cx, yy + 30),
                ctx.money(q.amount * p.qty, compact=True),
                "mono_bold" if cheap else "mono",
                13,
                t.accent2 if cheap else t.text,
                anchor="rs",
                max_w=vw - 8,
            )
            if cheap:
                c.text((cx, yy + 45), "cheapest", "mono", 9.5, t.accent2, anchor="rs")
        sell = p.best_sell_cash()
        if sell is not None:
            c.text((W - M, yy + 26), ctx.money(sell.net * p.qty, compact=True), "mono_bold", 14, t.good, anchor="rs")
            c.text((W - M, yy + 43), venue_label(sell.venue), "mono", 11, t.dim, anchor="rs")
        else:
            c.text((W - M, yy + 30), "no cash venue", "mono", 12, t.faint if t.dark else t.dim, anchor="rs")
        yy += 56
    c.line(M, yy, W - M, yy, mix(t.rule, t.panel, 0.3), 1)
    note = (
        "Totals per venue cover only the items that venue quotes (see coverage). Floors are listings, not "
        "offers to buy from you."
    )
    c.text((M, yy + 26), note, "mono", 11.5, t.faint if t.dark else t.dim, max_w=W - 2 * M)
    fy = trust_footer(c, ctx, yy + 44)
    return _crop(c, fy + 22)


# --------------------------------------------------------------------------------------
# Banner (README)
# --------------------------------------------------------------------------------------
def render_banner(
    theme: Theme, width: int = 1280, height: int = 360, report: Report | None = None, backend=None
) -> Image.Image:
    t = theme
    c = Canvas(width, height, t)
    if t.glow:
        c.glow((width * 0.78, height * 0.4), 360, t.accent, 40)
        c.glow((width * 0.2, height * 0.9), 300, t.accent2, 24)
    x = 72
    c.text((x, 96), "$ stashcard demo", "mono", 18, t.dim)
    c.rrect((x, 128, x + 22, 192), 3, fill=t.accent)
    c.text((x + 40, 186), "stashcard", "mono_xbold", 76, t.text)
    c.text((x, 244), "Value your Steam stash in the terminal.", "sans_semi", 24, t.text)
    c.text((x, 276), "Print it as a report card you can share.", "sans", 24, t.dim)
    c.text(
        (x, 322),
        "LOWEST = floor now · AVG = mean of real sales · every number labelled",
        "mono",
        13,
        t.faint if t.dark else t.dim,
    )
    if report is not None:
        ctx = Ctx(report, t, backend)
        # a stack of mini tiles on the right
        tiles = report.top(6)
        size = 92
        gx, gy = width - 72 - 3 * (size + 12) + 12, 70
        for i, p in enumerate(tiles):
            col, row = i % 3, i // 3
            bx = gx + col * (size + 12)
            by = gy + row * (size + 44)
            icon_tile(c, ctx, p, (bx, by, bx + size, by + size), radius=12)
            c.text(
                (bx + size / 2, by + size + 22),
                ctx.money(p.lowest.amount if p.lowest else None),
                "mono_bold",
                13,
                t.text,
                anchor="ms",
            )
    return c.finish()
