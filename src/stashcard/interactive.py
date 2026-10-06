"""The interactive flow: paste a profile, pick a mode with the arrow keys, tweak, done.

Every answer is remembered for next time (settings.json). Secrets are never asked for here;
use `stashcard config api-key` / `stashcard config cookie` for the optional extras.
"""

from __future__ import annotations

import argparse

import questionary
from questionary import Choice, Separator, Style

from stashcard.cli import (
    CARD_MODES,
    EXIT_INPUT,
    EXIT_NETWORK,
    EXIT_OK,
    UserError,
    _with_steps,
    after_save,
    cmd_demo,
    cmd_diff,
    emit_exports,
    resolve_redaction,
    runtime,
    save_cards,
    scan_options,
    scan_target,
)
from stashcard.config import Settings
from stashcard.demo import is_demo_ref
from stashcard.games import GAMES, parse_game
from stashcard.markets import VENUES
from stashcard.money import CURRENCIES
from stashcard.net import FetchError, NotFoundError, PrivateError, RateLimited
from stashcard.pricing import SOURCE_HELP
from stashcard.profile import ProfileInputError, parse_profile
from stashcard.redact import LEVELS

STYLE = Style(
    [
        ("qmark", "fg:#7CF0B6 bold"),
        ("question", "bold"),
        ("answer", "fg:#F5B94A bold"),
        ("pointer", "fg:#7CF0B6 bold"),
        ("highlighted", "fg:#7CF0B6 bold"),
        ("selected", "fg:#F5B94A noreverse"),
        ("instruction", "fg:#56637A"),
        ("separator", "fg:#56637A"),
        ("disabled", "fg:#56637A italic"),
    ]
)

MODES = [
    ("summary", "Report card", "account summary: totals, games, top items"),
    ("game", "Game inventory card", "one game in detail, rarity mix, days to sell"),
    ("grid", "Inventory picture", "a grid of item icons with prices"),
    ("receipt", "Receipt", "an itemised thermal-receipt card"),
    ("value", "Quick value", "terminal only, no image"),
    ("markets", "Marketplaces", "every venue's floor, net after fees, where to buy and sell"),
    ("diff", "Compare / then vs now", "two profiles side by side, or your last scan vs today"),
    ("demo", "Demo", "fictional profile, works offline"),
]
COMMON_CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "BRL", "PLN", "CNY", "TRY", "CHF", "RUB", "UAH", "KZT"]
REDACT_HELP = {
    "auto": "none for your own profile, full for others, light until you tell me which one is you",
    "none": "show name, avatar, profile link and exact totals",
    "light": "mask name and ID, blur avatar, drop the link",
    "full": "anonymous name and avatar, totals as value bands",
}


def _ask(q):
    ans = q.ask()
    if ans is None:  # Ctrl-C
        raise KeyboardInterrupt
    return ans


def _validate_profile(text: str):
    if not text.strip():
        return "Paste a profile URL, SteamID64, vanity name or 'demo'"
    if is_demo_ref(text) or text.strip().endswith("@last") or text.strip().endswith(".json"):
        return True
    try:
        parse_profile(text)
        return True
    except ProfileInputError as exc:
        return str(exc)


def _summary_line(s: Settings) -> str:
    games = ", ".join(GAMES[g].short if g in GAMES else g for g in s.games)
    extra = []
    if s.min_price:
        extra.append(f"min {s.min_price:g}")
    if s.tradable_only:
        extra.append("tradable only")
    if s.trimmed_avg:
        extra.append("trimmed AVG")
    tail = (" · " + ", ".join(extra)) if extra else ""
    venues = "+".join(VENUES[m].name.split(" ")[0] for m in s.markets if m in VENUES)
    return (
        f"{games} · {s.currency} · {venues} · LOWEST {s.lowest_from} · AVG {s.window} "
        f"· {s.theme} · redact {s.redact}{tail}"
    )


def _ask_settings(s: Settings, mode: str) -> None:
    choices = [Choice(f"{g.short:<8} {g.name}", value=k, checked=k in s.games) for k, g in GAMES.items()]
    choices.append(Choice("Custom appid/context ...", value="custom"))
    picked = _ask(
        questionary.checkbox("Games", choices=choices, style=STYLE, validate=lambda v: bool(v) or "Pick at least one")
    )
    games = [g for g in picked if g != "custom"]
    if "custom" in picked:

        def valid(t):
            try:
                parse_game(t)
                return True
            except ValueError as exc:
                return str(exc)

        custom = _ask(questionary.text("Custom appid/contextid (e.g. 252490/2)", validate=valid, style=STYLE))
        games.append(parse_game(custom).context)
    s.games = games or ["cs2"]

    cur_choices = [Choice(CURRENCIES[c].label(), value=c) for c in COMMON_CURRENCIES]
    cur_choices.append(Separator())
    cur_choices += [Choice(CURRENCIES[c].label(), value=c) for c in sorted(CURRENCIES) if c not in COMMON_CURRENCIES]
    s.currency = _ask(questionary.select("Currency", choices=cur_choices, default=s.currency, style=STYLE))
    s.source = _ask(
        questionary.select(
            "Price source",
            style=STYLE,
            default=s.source,
            choices=[Choice(f"{k:<9} {v}", value=k) for k, v in SOURCE_HELP.items()],
        )
    )
    market_choices = [
        Choice(
            f"{v.name:<13} {'wallet' if v.wallet else 'cash  '}  {v.note}"
            + ("  [experimental]" if v.experimental else ""),
            value=k,
            checked=k in s.markets,
        )
        for k, v in VENUES.items()
    ]
    s.markets = _ask(
        questionary.checkbox(
            "Marketplaces to compare",
            choices=market_choices,
            style=STYLE,
            validate=lambda v: bool(v) or "Pick at least one",
        )
    )
    floor_choices = [
        Choice("steam   Steam Market floor (default)", "steam"),
        Choice("best    cheapest floor across the venues you picked", "best"),
    ]
    floor_choices += [
        Choice(f"{k:<7} {VENUES[k].name} floor", k) for k in s.markets if k != "steam" and VENUES[k].kind == "floor"
    ]
    s.lowest_from = _ask(
        questionary.select(
            "LOWEST comes from",
            choices=floor_choices,
            style=STYLE,
            default=s.lowest_from if s.lowest_from in [c.value for c in floor_choices] else "steam",
        )
    )
    s.window = _ask(
        questionary.select(
            "AVG window (mean of real sales)",
            default=s.window,
            style=STYLE,
            choices=[
                Choice("24 hours", "24h"),
                Choice("7 days", "7d"),
                Choice("30 days", "30d"),
                Choice("90 days", "90d"),
            ],
        )
    )

    def valid_num(t):
        try:
            return float(t or 0) >= 0 or "Must be zero or more"
        except ValueError:
            return "Type a number like 0.5"

    s.min_price = float(
        _ask(
            questionary.text(
                f"Minimum price per item in {s.currency} (0 = everything)",
                default=f"{s.min_price:g}",
                validate=valid_num,
                style=STYLE,
            )
        )
        or 0
    )
    s.tradable_only = _ask(questionary.confirm("Only tradable items?", default=s.tradable_only, style=STYLE))
    if _ask(questionary.confirm("Advanced (trimmed AVG, capture rate, Skinport fee)?", default=False, style=STYLE)):
        s.trimmed_avg = _ask(
            questionary.confirm(
                "Use trimmed AVG (drops the highest and lowest sale, labelled)?", default=s.trimmed_avg, style=STYLE
            )
        )

        def valid_rate(t):
            try:
                return 0 < float(t) < 1 or "Between 0 and 1"
            except ValueError:
                return "A number like 0.1"

        s.capture = float(
            _ask(
                questionary.text(
                    "Capture rate for days-to-sell", default=f"{s.capture:g}", validate=valid_rate, style=STYLE
                )
            )
        )
        s.skinport_fee = float(
            _ask(
                questionary.text("Skinport seller fee", default=f"{s.skinport_fee:g}", validate=valid_rate, style=STYLE)
            )
        )
    if mode != "value":
        s.theme = _ask(
            questionary.select(
                "Card theme",
                default=s.theme,
                style=STYLE,
                choices=[
                    Choice("midnight  ink-dark with mint and amber", "midnight"),
                    Choice("paper     warm print, red and blue ink", "paper"),
                    Choice("phosphor  green CRT with scanlines", "phosphor"),
                ],
            )
        )
        s.redact = _ask(
            questionary.select(
                "Privacy on the card",
                default=s.redact,
                style=STYLE,
                choices=[Choice(f"{k:<6} {v}", k) for k, v in REDACT_HELP.items()],
            )
        )
        s.output_dir = (
            _ask(questionary.path("Save cards in", default=s.output_dir or ".", only_directories=True, style=STYLE))
            or "."
        )
        s.open_after = _ask(questionary.confirm("Open the image after saving?", default=s.open_after, style=STYLE))


def _namespace(s: Settings, mode: str, **extra) -> argparse.Namespace:
    ns = argparse.Namespace(
        game=list(s.games),
        currency=s.currency,
        source=s.source,
        window=s.window,
        min_price=f"{s.min_price}",
        tradable_only=s.tradable_only,
        trimmed_avg=s.trimmed_avg,
        capture=s.capture,
        skinport_fee=s.skinport_fee,
        steam_budget=60,
        offline=False,
        max_wait=20.0,
        markets=",".join(s.markets),
        lowest_from=None if s.lowest_from == "steam" else s.lowest_from,
        fee=None,
        csfloat_top=10,
        no_card=False,
        no_history=False,
        mode=mode,
        theme=s.theme,
        redact=s.redact,
        for_game=None,
        output=None,
        limit=36,
        paginate=False,
        open=s.open_after,
        preview="auto",
        no_preview=False,
        no_sidecar=False,
        json=None,
        csv=None,
        plain=False,
        top=12,
        quiet=False,
        profile="demo",
        out_dir=None,
        no_header=True,
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def run_interactive() -> int:
    from stashcard.term.ui import error, print_header, print_report

    rt = runtime()
    s = rt.settings
    print_header(rt.console)
    try:
        profile = _ask(
            questionary.text(
                "Steam profile (URL, SteamID64, STEAM_0:1:..., [U:1:...], vanity, or 'demo')",
                default=s.last_profile,
                validate=_validate_profile,
                style=STYLE,
            )
        )
        default_mode = "demo" if is_demo_ref(profile) else (s.mode if s.mode in [m[0] for m in MODES] else "summary")
        mode = _ask(
            questionary.select(
                "What do you want?",
                style=STYLE,
                default=default_mode,
                use_shortcuts=False,
                choices=[Choice(f"{title:<24} {desc}", value=key) for key, title, desc in MODES],
            )
        )
        other = None
        if mode == "diff":
            hint = f"{profile.strip()}@last" if not is_demo_ref(profile) else "demo2"
            other = _ask(
                questionary.text(
                    "Compare with (a second profile, a snapshot .json, or PROFILE@last for then vs now)",
                    default=hint,
                    validate=_validate_profile,
                    style=STYLE,
                )
            )
        if not _ask(questionary.confirm(f"Use saved settings? ({_summary_line(s)})", default=True, style=STYLE)):
            _ask_settings(s, mode)
        game_choice = None
        if mode == "game":
            options = [GAMES[g] if g in GAMES else parse_game(g) for g in s.games]
            game_choice = _ask(
                questionary.select("Which game?", style=STYLE, choices=[Choice(g.name, g.key) for g in options])
            )
        s.last_profile = profile.strip()
        s.mode = mode
        s.save()
    except KeyboardInterrupt:
        rt.console.print("\ncancelled")
        return 130

    try:
        if mode == "demo" or (is_demo_ref(profile) and mode not in ("diff",)):
            demo_mode = mode if mode in CARD_MODES + ("value", "markets") else "summary"
            args = _namespace(s, demo_mode, profile=profile.strip().split("@")[0] if is_demo_ref(profile) else "demo")
            if not args.game or set(args.game) == {"cs2", "dota2", "steam"}:
                args.game = ["cs2", "dota2", "tf2", "steam"]
            return cmd_demo(rt, args)
        if mode == "diff":
            args = _namespace(s, "diff", a=profile.strip(), b=other.strip())
            # "PROFILE@last" on the left means then (history) vs now (live)
            if other.strip().endswith("@last") and not profile.strip().endswith("@last"):
                args.a, args.b = other.strip(), profile.strip()
            return cmd_diff(rt, args)
        args = _namespace(s, mode, profile=profile.strip())
        opts = scan_options(args)
        report = _with_steps(rt, args, lambda steps: scan_target(rt, args.profile, opts, args, steps))
        if not s.me and not report.is_demo:
            mine = _ask(
                questionary.confirm(
                    f"Is {report.profile.persona} your own profile? (your cards then default to no redaction)",
                    default=False,
                    style=STYLE,
                )
            )
            if mine:
                s.me = report.profile.steamid64
                s.save()
        red = resolve_redaction(s.redact, report, s)
        if mode == "markets":
            from stashcard.term.ui import print_markets

            print_markets(rt.console, report, red, 10)
        else:
            print_report(rt.console, report, red, 12)
        emit_exports(rt, args, report, red)
        if mode == "value":
            return EXIT_OK
        game = GAMES.get(game_choice) if game_choice in GAMES else (parse_game(game_choice) if game_choice else None)
        paths = save_cards(rt, args, report, mode, red, game=game)
        after_save(rt, args, paths)
        return EXIT_OK
    except KeyboardInterrupt:
        rt.console.print("\ncancelled")
        return 130
    except UserError as exc:
        error(rt.console, str(exc), exc.hint)
        return exc.code
    except (ProfileInputError, NotFoundError) as exc:
        error(rt.console, str(exc), "Check the link, or pick Demo to see what Stashcard does offline.")
        return EXIT_INPUT
    except PrivateError as exc:
        error(rt.console, str(exc))
        return 4
    except (RateLimited, FetchError) as exc:
        error(rt.console, str(exc), "The Demo mode works offline.")
        return EXIT_NETWORK


__all__ = ["run_interactive", "LEVELS"]
