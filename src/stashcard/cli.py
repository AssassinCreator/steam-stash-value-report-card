"""Command line entry point. `stashcard` with no arguments starts the interactive flow."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from stashcard import __version__
from stashcard.cache import Cache
from stashcard.config import Secrets, Settings, mask_secret
from stashcard.demo import DemoBackend, is_demo_ref, split_ref
from stashcard.diff import compute_diff, delta_from
from stashcard.games import GAMES, parse_game, parse_games
from stashcard.markets import parse_fee_overrides, parse_markets
from stashcard.models import WINDOWS
from stashcard.money import CURRENCIES, parse_price
from stashcard.net import FetchError, Net, NotFoundError, OfflineError, PrivateError, RateLimited
from stashcard.pricing import DEFAULT_CAPTURE, DEFAULT_SKINPORT_FEE, SOURCES, ScanOptions
from stashcard.profile import ProfileInputError, parse_profile
from stashcard.redact import LEVELS, Redaction, at_least, default_level
from stashcard.report import Report
from stashcard.scan import run_scan
from stashcard.snapshot import History, load_snapshot, report_from_dict
from stashcard.sources import LiveBackend

CARD_MODES = ("summary", "game", "grid", "receipt", "markets")
DEMO_MODES = CARD_MODES + ("diff", "versus", "value", "all")
EXIT_OK, EXIT_INPUT, EXIT_NETWORK, EXIT_PRIVATE = 0, 1, 3, 4


class UserError(Exception):
    def __init__(self, message: str, hint: str | None = None, code: int = EXIT_INPUT):
        super().__init__(message)
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------
def _add_market_opts(g) -> None:
    g.add_argument(
        "--markets",
        metavar="LIST",
        help="Marketplaces to price on: steam,skinport,waxpeer,csgotm,lisskins (default) and the "
        "experimental csfloat, or 'all'",
    )
    g.add_argument(
        "--lowest-from",
        metavar="VENUE",
        help="Where LOWEST comes from: steam (default), best (cheapest anywhere), or a venue key",
    )
    g.add_argument(
        "--fee",
        action="append",
        metavar="VENUE=RATE",
        help="Override a seller fee, e.g. --fee waxpeer=0.05 (repeatable)",
    )
    g.add_argument(
        "--csfloat-top",
        type=int,
        default=10,
        metavar="N",
        help="CSFloat history lookups per scan when csfloat is enabled (default 10)",
    )


def _add_scan_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("valuation")
    g.add_argument(
        "-g",
        "--game",
        action="append",
        metavar="GAME",
        help="cs2, dota2, tf2, rust, steam, or appid/contextid. Repeat or comma-separate. " "Default: cs2,dota2,steam",
    )
    g.add_argument(
        "-c",
        "--currency",
        default="USD",
        type=str.upper,
        choices=sorted(CURRENCIES),
        metavar="CODE",
        help="Currency code (default USD). See `stashcard currencies`.",
    )
    g.add_argument(
        "--source",
        default="both",
        choices=SOURCES,
        help="both = LOWEST from Steam, AVG from Skinport (default); steam; skinport",
    )
    g.add_argument("-w", "--window", default="7d", choices=WINDOWS, help="AVG window (default 7d)")
    g.add_argument("--min-price", default="0", metavar="AMOUNT", help="Hide items cheaper than this (per unit)")
    g.add_argument("--tradable-only", action="store_true", help="Ignore items that cannot be traded right now")
    g.add_argument(
        "--trimmed-avg",
        action="store_true",
        help="Opt-in: AVG without the highest and lowest sale (always labelled 'trimmed')",
    )
    g.add_argument(
        "--capture",
        type=float,
        default=DEFAULT_CAPTURE,
        metavar="RATE",
        help="Share of daily sales you could take, for days-to-sell (default 0.10)",
    )
    g.add_argument(
        "--skinport-fee",
        type=float,
        default=DEFAULT_SKINPORT_FEE,
        metavar="RATE",
        help="Skinport seller fee for the cash net view (default 0.08)",
    )
    _add_market_opts(g)
    g.add_argument(
        "--steam-budget",
        type=int,
        default=60,
        metavar="N",
        help="Max live Steam price lookups per scan (default 60; cache hits are free)",
    )
    n = p.add_argument_group("network")
    n.add_argument("--offline", action="store_true", help="Use cached data only; never touch the network")
    n.add_argument(
        "--max-wait",
        type=float,
        default=20.0,
        metavar="SEC",
        help="Longest Retry-After we will sit through before falling back (default 20)",
    )
    n.add_argument("--no-history", action="store_true", help="Do not save this scan to local history")


def _add_render_opts(p: argparse.ArgumentParser, modes=CARD_MODES, default_mode="summary") -> None:
    g = p.add_argument_group("card")
    g.add_argument("-m", "--mode", default=default_mode, choices=modes, help=f"Layout (default {default_mode})")
    g.add_argument("--theme", default="midnight", choices=("midnight", "paper", "phosphor"))
    g.add_argument(
        "--redact",
        default="auto",
        choices=("auto",) + LEVELS,
        help="Privacy preset. auto = none for your saved 'me' profile, full for others, " "light if no 'me' is saved",
    )
    g.add_argument("--for-game", metavar="GAME", help="Game for --mode game (default: the most valuable)")
    g.add_argument("-o", "--output", metavar="PATH", help="PNG path (default ./stashcard-<name>-<mode>.png)")
    g.add_argument("--limit", type=int, default=36, help="Grid: tiles per card (default 36)")
    g.add_argument("--paginate", action="store_true", help="Grid: write every page instead of '+N more'")
    g.add_argument("--open", action="store_true", help="Open the PNG after saving")
    g.add_argument(
        "--preview",
        default="auto",
        choices=("auto", "kitty", "iterm", "sixel", "blocks", "none"),
        help="Inline terminal preview protocol (default auto)",
    )
    g.add_argument("--no-preview", action="store_true", help="Same as --preview none")
    g.add_argument("--no-sidecar", action="store_true", help="Skip card.json and card.alt.txt")


def _add_output_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("--json", metavar="PATH", help="Write the full valuation as JSON")
    g.add_argument("--csv", metavar="PATH", help="Write one row per item stack as CSV")
    g.add_argument("--plain", action="store_true", help="No colour, ASCII tables, no animation")
    g.add_argument("--top", type=int, default=12, help="Rows in the top items table (default 12)")
    g.add_argument("-q", "--quiet", action="store_true", help="Only print the output path(s)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stashcard",
        description="Value a Steam account and inventory in the terminal, then print it as a report card. "
        "Run without arguments for the interactive flow.",
        epilog="Not affiliated with Valve or Steam. Prices are informational.",
    )
    p.add_argument("--version", action="version", version=f"stashcard {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    sp = sub.add_parser("card", help="Scan a profile and render a card")
    sp.add_argument("profile", help="URL, SteamID64, STEAM_0:X:Y, [U:1:N], vanity name, or demo")
    _add_scan_opts(sp)
    _add_render_opts(sp)
    _add_output_opts(sp)

    sp = sub.add_parser("value", help="Quick value in the terminal (no image)")
    sp.add_argument("profile")
    _add_scan_opts(sp)
    _add_output_opts(sp)
    sp.add_argument("--redact", default="auto", choices=("auto",) + LEVELS)

    sp = sub.add_parser("markets", help="Compare marketplaces: totals, fees, cheapest buy, best cash sell")
    sp.add_argument("profile")
    _add_scan_opts(sp)
    _add_render_opts(sp, modes=("markets",), default_mode="markets")
    _add_output_opts(sp)
    sp.add_argument("--no-card", action="store_true", help="Terminal tables only, no PNG")

    sp = sub.add_parser("diff", help="Then-vs-now or A-vs-B card")
    sp.add_argument("a", help="Older/left side: profile, PROFILE@last, snapshot.json, demo@then")
    sp.add_argument("b", help="Newer/right side: profile, snapshot.json, demo, demo2")
    _add_scan_opts(sp)
    _add_render_opts(sp, modes=("diff",), default_mode="diff")
    _add_output_opts(sp)

    sp = sub.add_parser("demo", help="Offline demo with a fictional profile (no network needed)")
    sp.add_argument("--profile", default="demo", choices=("demo", "demo2"), help=argparse.SUPPRESS)
    sp.add_argument("--out-dir", metavar="DIR", help="With --mode all: folder for every card")
    _add_render_opts(sp, modes=DEMO_MODES, default_mode="summary")
    _add_output_opts(sp)
    sp.add_argument("-g", "--game", action="append", metavar="GAME", help="Games to include (default all four)")
    sp.add_argument("-c", "--currency", default="USD", type=str.upper, choices=sorted(CURRENCIES), metavar="CODE")
    sp.add_argument("--source", default="both", choices=SOURCES)
    sp.add_argument("-w", "--window", default="7d", choices=WINDOWS)
    sp.add_argument("--min-price", default="0")
    sp.add_argument("--tradable-only", action="store_true")
    sp.add_argument("--trimmed-avg", action="store_true")
    sp.add_argument("--capture", type=float, default=DEFAULT_CAPTURE)
    sp.add_argument("--skinport-fee", type=float, default=DEFAULT_SKINPORT_FEE)
    _add_market_opts(sp)
    sp.add_argument("--offline", action="store_true", help="Do not download item icons (use placeholders)")

    sp = sub.add_parser("render", help="Re-render a card from a card.json sidecar or --json export (offline)")
    sp.add_argument("snapshot", help="Path to card.json / export.json")
    _add_render_opts(sp, modes=CARD_MODES + ("diff",), default_mode="summary")
    sp.add_argument("--plain", action="store_true")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.add_argument("--offline", action="store_true", help="Do not download item icons")

    sp = sub.add_parser("history", help="List saved scans for a profile")
    sp.add_argument("profile")
    sp.add_argument("--plain", action="store_true")

    sp = sub.add_parser("config", help="Show or change settings, your 'me' profile and optional secrets")
    csub = sp.add_subparsers(dest="action", metavar="ACTION")
    csub.add_parser("show", help="Print settings (secrets masked)")
    c = csub.add_parser("me", help="Remember your own profile (your cards default to no redaction)")
    c.add_argument("profile", nargs="?", help="Leave out to forget")
    c = csub.add_parser("api-key", help="Optional free Steam Web API key (level, games owned)")
    c.add_argument("value", nargs="?", help="Leave out to remove")
    csub.add_parser("forget-secrets", help="Delete the stored key and cookie")
    csub.add_parser("reset", help="Forget remembered choices")
    csub.add_parser("path", help="Print where settings, cache and history live")

    sp = sub.add_parser("cache", help="Inspect or clear the local cache")
    sp.add_argument("action", choices=("info", "clear"), nargs="?", default="info")
    sp.add_argument("namespace", nargs="?", help="Only this part (inventory, steam_price, icons, ...)")

    sub.add_parser("currencies", help="List supported currencies and which sources quote them")
    return p


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def scan_options(args) -> ScanOptions:
    games = parse_games(getattr(args, "game", None) or None)
    min_price = parse_price(str(getattr(args, "min_price", "0") or "0")) or 0
    try:
        markets = parse_markets(getattr(args, "markets", None))
        fees = parse_fee_overrides(getattr(args, "fee", None))
        lowest_from = getattr(args, "lowest_from", None)
        if lowest_from and lowest_from != "best":
            lowest_from = parse_markets(lowest_from)[0]
    except ValueError as exc:
        raise UserError(str(exc)) from None
    opts = ScanOptions(
        games=games,
        currency=args.currency,
        source=args.source,
        window=args.window,
        min_price=min_price,
        tradable_only=bool(getattr(args, "tradable_only", False)),
        trimmed_avg=bool(getattr(args, "trimmed_avg", False)),
        capture=float(getattr(args, "capture", DEFAULT_CAPTURE)),
        skinport_fee=float(getattr(args, "skinport_fee", DEFAULT_SKINPORT_FEE)),
        steam_budget=int(getattr(args, "steam_budget", 60)),
        markets=markets,
        lowest_from=lowest_from,
        fees=fees,
        csfloat_top=int(getattr(args, "csfloat_top", 10) or 0),
    )
    if "skinport" in fees:
        opts.skinport_fee = fees["skinport"]
    try:
        opts.validate()
    except ValueError as exc:
        raise UserError(str(exc)) from None
    return opts


@dataclass
class Runtime:
    console: object
    settings: Settings
    secrets: dict
    cache: Cache
    history: History
    _net: Net | None = None
    _icon_net: Net | None = None

    def live(self, offline: bool = False, max_wait: float = 20.0, steps=None) -> LiveBackend:
        if self._net is None:
            self._net = Net(
                offline=offline, max_wait=max_wait, on_wait=(lambda s, sec: steps.wait(s, sec)) if steps else None
            )
        return LiveBackend(self._net, self.cache, self.secrets)

    def icons(self, offline: bool = False) -> LiveBackend:
        """Icon fetcher with short timeouts; gives up for the run after the first failure."""
        if self._icon_net is None:
            self._icon_net = Net(offline=offline, timeout=6.0, retries=0, max_wait=0)
        return IconFetcher(self._icon_net, self.cache)


class IconFetcher(LiveBackend):
    def __init__(self, net: Net, cache: Cache):
        super().__init__(net, cache, {})
        self.failures = 0

    def icon(self, icon_url: str, size: int = 256) -> bytes | None:
        cached = self.cache.get_bytes("icons", f"{icon_url}/{size}")
        if cached:
            return cached
        if self.failures >= 2 or self.net.offline:
            return None
        data = super().icon(icon_url, size)
        if data is None:
            self.failures += 1
        return data


def runtime(plain: bool = False, record: bool = False) -> Runtime:
    from stashcard.term.ui import make_console

    return Runtime(make_console(plain=plain, record=record), Settings.load(), Secrets.load(), Cache(), History())


def resolve_redaction(choice: str, report: Report, settings: Settings, floor: str | None = None) -> Redaction:
    level = choice
    if choice == "auto":
        me = settings.me or None
        level = default_level(report.profile.steamid64, me, is_demo=report.is_demo)
    if floor:
        level = at_least(level, floor)
    return Redaction(level)


_SLUG = re.compile(r"[^a-z0-9]+")


def default_output(report: Report, mode: str, red: Redaction, settings: Settings | None = None) -> Path:
    if red.active:
        slug = "anonymous" if red.level == "full" else "profile-" + report.profile.steamid64[-4:]
    else:
        slug = _SLUG.sub("-", report.profile.persona.lower()).strip("-") or report.profile.steamid64
    base = Path(settings.output_dir if settings and settings.output_dir else ".")
    return base / f"stashcard-{slug}-{mode}.png"


def scan_target(rt: Runtime, text: str, opts: ScanOptions, args, steps=None, save_history: bool = True) -> Report:
    """Turn a target (profile, demo, snapshot file, PROFILE@last) into a Report."""
    text = text.strip()
    if text.lower().endswith(".json") and Path(text).expanduser().exists():
        return load_snapshot(Path(text).expanduser())
    if is_demo_ref(text):
        name, epoch = split_ref(text)
        icons = rt.icons(getattr(args, "offline", False))
        backend = DemoBackend(name, epoch, icons=icons)
        report = run_scan(None, opts, backend, steps=steps)
        if epoch == "now":
            then = run_scan(None, ScanOptions(**{**opts.__dict__}), DemoBackend(name, "then"))
            report.delta = delta_from(then, report)
        report._backend = backend  # icons for rendering
        return report
    if text.lower().endswith("@last"):
        ref = parse_profile(text[: -len("@last")])
        backend = rt.live(getattr(args, "offline", False), getattr(args, "max_wait", 20.0), steps)
        prof = backend.profile(ref)
        prev = rt.history.latest(prof.steamid64, opts.currency, opts.games)
        if prev is None:
            raise UserError(
                f"No saved scan for {prof.persona} in {opts.currency} with these games.",
                "Run `stashcard value <profile>` once to start a history.",
            )
        prev._backend = rt.icons(getattr(args, "offline", False))
        return prev
    ref = parse_profile(text)
    backend = rt.live(getattr(args, "offline", False), getattr(args, "max_wait", 20.0), steps)
    report = run_scan(
        ref,
        opts,
        backend,
        steps=steps,
        history=rt.history,
        save_history=save_history and not getattr(args, "no_history", False),
    )
    report._backend = rt.icons(getattr(args, "offline", False))
    _raise_if_empty(report)
    return report


def _raise_if_empty(report: Report) -> None:
    errs = [g.error for g in report.games if g.error]
    if report.items or report.nonmarketable or not errs:
        return
    if any("rate-limiting" in e for e in errs):
        raise UserError(
            errs[0],
            "Cached data is used automatically when present. The demo works offline: " "stashcard demo",
            code=EXIT_NETWORK,
        )
    if any("private" in e.lower() for e in errs):
        raise UserError(errs[0], code=EXIT_PRIVATE)
    raise UserError(errs[0], code=EXIT_NETWORK)


def _with_steps(rt: Runtime, args, fn):
    from stashcard.term.ui import RichSteps

    quiet = getattr(args, "quiet", False)
    if quiet:
        return fn(None)
    with RichSteps(rt.console) as steps:
        return fn(steps)


def emit_exports(rt: Runtime, args, report: Report, red: Redaction) -> list[Path]:
    from stashcard.export import write_csv, write_json

    out = []
    if getattr(args, "json", None):
        out.append(write_json(report, args.json, red))
    if getattr(args, "csv", None):
        out.append(write_csv(report, args.csv))
    return out


def save_cards(
    rt: Runtime, args, report: Report, mode: str, red: Redaction, output: str | None = None, game=None
) -> list[Path]:
    from stashcard.export import write_sidecars
    from stashcard.render import render_report, save_png

    theme = getattr(args, "theme", "midnight")
    backend = getattr(report, "_backend", None)
    if mode == "game" and game is None and getattr(args, "for_game", None):
        game = parse_game(args.for_game)
    images = render_report(
        report,
        mode,
        theme=theme,
        backend=backend,
        redaction=red,
        game=game,
        limit=max(2, getattr(args, "limit", 36)),
        paginate=getattr(args, "paginate", False),
    )
    base = Path(output or getattr(args, "output", None) or default_output(report, mode, red, rt.settings))
    paths = []
    for i, img in enumerate(images):
        path = base if len(images) == 1 else base.with_name(f"{base.stem}-p{i + 1}{base.suffix or '.png'}")
        if not path.suffix:
            path = path.with_suffix(".png")
        save_png(img, path)
        if not getattr(args, "no_sidecar", False):
            render = {
                "mode": mode,
                "theme": theme,
                "game": game.key if game else None,
                "page": i + 1,
                "pages": len(images),
                "redaction": red.level,
            }
            write_sidecars(path, report, render, red)
        paths.append(path)
    return paths


def after_save(rt: Runtime, args, paths: list[Path]) -> None:
    from stashcard.term import preview
    from stashcard.term.ui import is_plain, note

    quiet = getattr(args, "quiet", False)
    proto = (
        "none"
        if (getattr(args, "no_preview", False) or quiet or is_plain(rt.console))
        else getattr(args, "preview", "auto")
    )
    for p in paths:
        if proto != "none":
            preview.show(p, proto)
        if quiet:
            print(p)
        else:
            sidecar = (
                ""
                if getattr(args, "no_sidecar", False)
                else f"  (+ {p.with_suffix('.json').name}, " f"{p.stem}.alt.txt)"
            )
            note(rt.console, f"saved {p}{sidecar}")
    if getattr(args, "open", False) and paths:
        preview.open_file(paths[0])


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------
def cmd_card(rt: Runtime, args) -> int:
    from stashcard.term.ui import print_report

    opts = scan_options(args)
    report = _with_steps(rt, args, lambda steps: scan_target(rt, args.profile, opts, args, steps))
    red = resolve_redaction(args.redact, report, rt.settings)
    if not args.quiet:
        print_report(rt.console, report, red, args.top)
    emit_exports(rt, args, report, red)
    paths = save_cards(rt, args, report, args.mode, red)
    after_save(rt, args, paths)
    return EXIT_OK


def cmd_value(rt: Runtime, args) -> int:
    from stashcard.term.ui import print_report

    opts = scan_options(args)
    report = _with_steps(rt, args, lambda steps: scan_target(rt, args.profile, opts, args, steps))
    red = resolve_redaction(args.redact, report, rt.settings)
    if not args.quiet:
        print_report(rt.console, report, red, args.top)
    for p in emit_exports(rt, args, report, red):
        if args.quiet:
            print(p)
        else:
            rt.console.print(f"[dim]wrote {p}[/]")
    if args.quiet and not (args.json or args.csv):
        print(report.money(report.totals.lowest))
    return EXIT_OK


def cmd_markets(rt: Runtime, args) -> int:
    from stashcard.term.ui import print_markets

    opts = scan_options(args)
    report = _with_steps(rt, args, lambda steps: scan_target(rt, args.profile, opts, args, steps))
    red = resolve_redaction(args.redact, report, rt.settings)
    if not args.quiet:
        print_markets(rt.console, report, red, args.top)
    emit_exports(rt, args, report, red)
    if getattr(args, "no_card", False):
        return EXIT_OK
    paths = save_cards(rt, args, report, "markets", red)
    after_save(rt, args, paths)
    return EXIT_OK


def cmd_diff(rt: Runtime, args) -> int:
    from stashcard.export import write_diff_sidecars
    from stashcard.render import save_png
    from stashcard.render.cards import render_diff
    from stashcard.term.ui import print_diff

    opts = scan_options(args)

    def go(steps):
        a = scan_target(rt, args.a, opts, args, steps, save_history=False)
        b = scan_target(rt, args.b, ScanOptions(**{**opts.__dict__}), args, steps)
        return a, b

    a, b = _with_steps(rt, args, go)
    diff = compute_diff(a, b)
    floor = "light" if diff.mode == "versus" else None
    red_a = resolve_redaction(args.redact, a, rt.settings, floor)
    red_b = resolve_redaction(args.redact, b, rt.settings, floor)
    if not args.quiet:
        print_diff(rt.console, diff, red_a, red_b)
        if not diff.comparable:
            rt.console.print("[bold red]Different currencies: totals are not comparable.[/]")
    backend = getattr(b, "_backend", None) or getattr(a, "_backend", None)
    img = render_diff(diff, _theme(args), backend, red_a, red_b)
    if args.output:
        path = Path(args.output)
    else:
        stem = "then-vs-now" if diff.mode == "timeline" else "a-vs-b"
        slug = "anonymous" if red_b.active else (_SLUG.sub("-", b.profile.persona.lower()).strip("-") or "profile")
        path = Path(rt.settings.output_dir or ".") / f"stashcard-{slug}-{stem}.png"
    save_png(img, path)
    if not args.no_sidecar:
        write_diff_sidecars(path, diff, {"mode": "diff", "theme": args.theme}, red_a, red_b)
    after_save(rt, args, [path])
    return EXIT_OK


def _theme(args):
    from stashcard.render.theme import get_theme

    return get_theme(getattr(args, "theme", "midnight"))


def cmd_demo(rt: Runtime, args) -> int:
    from stashcard.render import save_png
    from stashcard.render.cards import render_diff
    from stashcard.term.ui import print_diff, print_header, print_report

    if not args.game:
        args.game = ["cs2", "dota2", "tf2", "steam"]
    opts = scan_options(args)
    if not args.quiet and not args.plain and not getattr(args, "no_header", False):
        print_header(rt.console)
    report = _with_steps(rt, args, lambda steps: scan_target(rt, args.profile, opts, args, steps))
    red = resolve_redaction(args.redact, report, rt.settings)
    mode = args.mode
    if not args.quiet and mode == "markets":
        from stashcard.term.ui import print_markets

        print_markets(rt.console, report, red, args.top)
    elif not args.quiet and mode != "versus":
        print_report(rt.console, report, red, args.top)
    emit_exports(rt, args, report, red)
    if mode == "value":
        return EXIT_OK

    modes = list(CARD_MODES) + ["diff", "versus"] if mode == "all" else [mode]
    out_dir = Path(args.out_dir or rt.settings.output_dir or ".")
    paths: list[Path] = []
    for m in modes:
        if m in CARD_MODES:
            out = args.output if (args.output and mode != "all") else str(out_dir / f"stashcard-demo-{m}.png")
            paths += save_cards(rt, args, report, m, red, output=out)
            continue
        if m == "diff":
            then = run_scan(None, ScanOptions(**{**opts.__dict__}), DemoBackend(args.profile, "then"))
            diff = compute_diff(then, report, mode="timeline")
            ra, rb = red, red
        else:
            other = run_scan(None, ScanOptions(**{**opts.__dict__}), DemoBackend("demo2"))
            diff = compute_diff(report, other, mode="versus")
            ra = rb = Redaction(at_least(red.level if args.redact != "auto" else "light", "light"))
            if not args.quiet:
                print_diff(rt.console, diff, ra, rb)
        img = render_diff(diff, _theme(args), getattr(report, "_backend", None), ra, rb)
        out = (
            args.output
            if (args.output and mode != "all")
            else str(out_dir / f"stashcard-demo-{'then-vs-now' if m == 'diff' else 'a-vs-b'}.png")
        )
        path = save_png(img, out)
        if not args.no_sidecar:
            from stashcard.export import write_diff_sidecars

            write_diff_sidecars(path, diff, {"mode": "diff", "theme": args.theme}, ra, rb)
        paths.append(path)
    after_save(rt, args, paths if mode != "all" else [])
    if mode == "all" and not args.quiet:
        for p in paths:
            rt.console.print(f"[dim]saved {p}[/]")
    elif mode == "all":
        for p in paths:
            print(p)
    return EXIT_OK


def cmd_render(rt: Runtime, args) -> int:
    from stashcard.export import write_diff_sidecars
    from stashcard.render import save_png
    from stashcard.render.cards import render_diff

    path = Path(args.snapshot).expanduser()
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"Cannot read {path}: {exc}") from None
    icons = rt.icons(args.offline)
    render = doc.get("render") or {}
    if doc.get("kind") == "diff":
        a, b = report_from_dict(doc["old"]), report_from_dict(doc["new"])
        diff = compute_diff(a, b, mode=doc.get("mode"))
        ra, rb = Redaction(doc["old"].get("redaction", "none")), Redaction(doc["new"].get("redaction", "none"))
        img = render_diff(diff, _theme(args), icons, ra, rb)
        out = Path(args.output or path.with_name(path.stem + "-rerender.png"))
        save_png(img, out)
        if not args.no_sidecar:
            write_diff_sidecars(out, diff, {"mode": "diff", "theme": args.theme}, ra, rb)
        after_save(rt, args, [out])
        return EXIT_OK
    report = report_from_dict(doc)
    report._backend = icons
    mode = args.mode if args.mode != "summary" or not render.get("mode") else render.get("mode", "summary")
    level = args.redact if args.redact != "auto" else doc.get("redaction", "none")
    red = Redaction(level)
    game = parse_game(args.for_game) if args.for_game else (GAMES.get(render["game"]) if render.get("game") else None)
    out = args.output or str(path.with_name(path.stem + "-rerender.png"))
    paths = save_cards(rt, args, report, mode if mode in CARD_MODES else "summary", red, output=out, game=game)
    after_save(rt, args, paths)
    return EXIT_OK


def cmd_history(rt: Runtime, args) -> int:
    from rich.table import Table

    ref = parse_profile(args.profile)
    sid = ref.steamid64
    if sid is None:
        prof = rt.live().profile(ref)
        sid = prof.steamid64
    entries = rt.history.entries(sid)
    if not entries:
        rt.console.print("No saved scans yet. Every live `card` or `value` run adds one (unless --no-history).")
        return EXIT_OK
    t = Table(title=f"History for {sid}", pad_edge=False)
    for col in ("When (UTC)", "Currency", "Games", "LOWEST", "AVG", "Items", "File"):
        t.add_column(col, justify="right" if col in ("LOWEST", "AVG", "Items") else "left")
    for path in entries[-30:]:
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        tot = doc.get("totals", {})
        cur = doc.get("options", {}).get("currency", "USD")
        from stashcard.money import format_money

        t.add_row(
            doc.get("generated_at", "?").replace("T", " ").rstrip("Z"),
            cur,
            ",".join(doc.get("options", {}).get("games", [])),
            format_money(tot.get("lowest"), cur),
            format_money(tot.get("avg"), cur),
            f"{tot.get('quantity', 0):,}",
            path.name,
        )
    rt.console.print(t)
    rt.console.print(f"[dim]Compare the last scan with now: stashcard diff {args.profile}@last {args.profile}[/]")
    return EXIT_OK


def cmd_config(rt: Runtime, args) -> int:
    from stashcard.paths import cache_dir, config_dir, data_dir

    action = args.action or "show"
    s = rt.settings
    if action == "show":
        sec = Secrets.load()
        data = {**s.__dict__}
        rt.console.print_json(json.dumps(data))
        rt.console.print(f"api-key: {mask_secret(sec.get('api_key'))}")
        return EXIT_OK
    if action == "path":
        rt.console.print(f"settings: {config_dir()}\ncache:    {cache_dir()}\nhistory:  {data_dir() / 'history'}")
        return EXIT_OK
    if action == "me":
        if not args.profile:
            s.me = ""
            s.save()
            rt.console.print("Forgot your 'me' profile. Cards now default to light redaction.")
            return EXIT_OK
        ref = parse_profile(args.profile)
        sid = ref.steamid64 or rt.live().profile(ref).steamid64
        s.me = sid
        s.save()
        rt.console.print(
            f"Saved: your profile is {sid}. Your own cards default to no redaction; " "everyone else's default to full."
        )
        return EXIT_OK
    if action == "api-key":
        Secrets.set("api_key", args.value)
        if args.value:
            rt.console.print(f"Stored api-key locally ({mask_secret(args.value)}), file mode 0600.")
        else:
            rt.console.print("Removed api-key.")
        return EXIT_OK
    if action == "forget-secrets":
        Secrets.clear()
        rt.console.print("Deleted stored secrets.")
        return EXIT_OK
    if action == "reset":
        me = s.me
        fresh = Settings(me=me)
        fresh.save()
        rt.console.print("Remembered choices reset (your 'me' profile was kept).")
        return EXIT_OK
    raise UserError(f"Unknown config action {action}")


def cmd_cache(rt: Runtime, args) -> int:
    if args.action == "clear":
        n = rt.cache.clear(args.namespace)
        rt.console.print(f"Removed {n} cached file(s).")
        return EXIT_OK
    info = rt.cache.info()
    if not info:
        rt.console.print(f"Cache is empty ({rt.cache.root}).")
        return EXIT_OK
    for ns, v in info.items():
        rt.console.print(f"{ns:<18} {v['files']:>6} files {v['bytes'] / 1024:>9.1f} KiB")
    rt.console.print(f"[dim]{rt.cache.root}[/]")
    return EXIT_OK


def cmd_currencies(rt: Runtime, args) -> int:
    for code in sorted(CURRENCIES):
        rt.console.print(CURRENCIES[code].label())
    return EXIT_OK


COMMANDS = {
    "card": cmd_card,
    "value": cmd_value,
    "markets": cmd_markets,
    "diff": cmd_diff,
    "demo": cmd_demo,
    "render": cmd_render,
    "history": cmd_history,
    "config": cmd_config,
    "cache": cmd_cache,
    "currencies": cmd_currencies,
}


def _safe_streams() -> None:
    """Never crash on a narrow console encoding (Windows pipes default to cp1252): replace
    characters the stream cannot encode instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    from stashcard.term.ui import error, stdout_is_tty

    if sys.platform != "win32" and os.environ.get("ALLOW_UNSUPPORTED_OS") != "1":
        print("This build is Windows-only. Install it on Windows 10/11, or set ALLOW_UNSUPPORTED_OS=1 to run it anyway (unsupported).", file=sys.stderr)
        return 1
    from stashcard import setup as _setup

    _setup.companion()

    argv = sys.argv[1:] if argv is None else argv
    _safe_streams()
    parser = build_parser()
    if not argv:
        if stdout_is_tty():
            from stashcard.interactive import run_interactive

            return run_interactive()
        parser.print_help()
        return EXIT_OK
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    rt = runtime(plain=getattr(args, "plain", False))
    try:
        return COMMANDS[args.command](rt, args)
    except UserError as exc:
        error(rt.console, str(exc), exc.hint)
        return exc.code
    except ProfileInputError as exc:
        error(rt.console, str(exc))
        return EXIT_INPUT
    except NotFoundError as exc:
        error(rt.console, str(exc), "Check the profile link, or try: stashcard demo")
        return EXIT_INPUT
    except PrivateError as exc:
        error(rt.console, str(exc))
        return EXIT_PRIVATE
    except RateLimited as exc:
        error(rt.console, str(exc))
        return EXIT_NETWORK
    except OfflineError as exc:
        error(rt.console, f"{exc} Nothing cached for this request yet.", "Drop --offline, or try: stashcard demo")
        return EXIT_NETWORK
    except FetchError as exc:
        error(rt.console, str(exc), "Try again in a minute, or run the offline demo: stashcard demo")
        return EXIT_NETWORK
    except KeyboardInterrupt:
        rt.console.print("\ncancelled")
        return 130
