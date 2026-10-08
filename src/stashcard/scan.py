"""One scan, start to finish: profile -> inventories -> prices -> Report (+ delta)."""

from __future__ import annotations

from stashcard.diff import delta_from
from stashcard.inventory import join_inventory
from stashcard.models import Item
from stashcard.net import FetchError, OfflineError, RateLimited
from stashcard.pricing import Notice, PriceEngine, ScanOptions
from stashcard.profile import ProfileRef
from stashcard.report import GameTotals, Report, build_report
from stashcard.snapshot import History


class Steps:
    """Progress hooks. The terminal UI implements these with Rich; tests use the no-op."""

    def start(self, key: str, label: str, total: int | None = None) -> None: ...

    def update(
        self, key: str, *, label: str | None = None, completed: int | None = None, total: int | None = None
    ) -> None: ...

    def done(self, key: str, label: str | None = None, ok: bool = True) -> None: ...

    def wait(self, service: str, seconds: float) -> None: ...


def run_scan(
    ref: ProfileRef | None,
    opts: ScanOptions,
    backend,
    *,
    steps: Steps | None = None,
    history: History | None = None,
    save_history: bool = True,
    previous: Report | None = None,
) -> Report:
    steps = steps or Steps()
    opts.validate()

    steps.start("profile", "Reading profile")
    profile = backend.profile(ref)
    steps.done("profile", f"Profile: {profile.persona}")

    notices: list[Notice] = []
    games: list[GameTotals] = []
    marketable: list[Item] = []
    nonmarketable: list[Item] = []
    for game in opts.games:
        key = f"inv:{game.key}"
        steps.start(key, f"Inventory {game.short}")
        gt = GameTotals(game=game)
        games.append(gt)
        try:
            res = backend.inventory(profile.steamid64, game)
        except RateLimited as exc:
            gt.error = str(exc)
            steps.done(key, f"Inventory {game.short}: rate limited", ok=False)
            continue
        except (FetchError, OfflineError) as exc:
            gt.error = str(exc)
            steps.done(key, f"Inventory {game.short}: {exc}", ok=False)
            continue
        if res.age is not None and (res.stale or res.age > 60):
            when = f"{_age(res.age)} old"
            if res.stale:
                notices.append(Notice("cache", f"{game.short} inventory is from cache ({when}): {res.note}."))
            gt_note = f" (cached, {when})"
        else:
            gt_note = ""
        stacks = join_inventory(res.data, game, tradable_only=opts.tradable_only)
        for it in stacks:
            (marketable if it.marketable else nonmarketable).append(it)
        qty = sum(i.quantity for i in stacks)
        steps.done(key, f"Inventory {game.short}: {qty:,} items in {len(stacks)} stacks{gt_note}")

    steps.start("prices", "Pricing", total=None)

    def progress(label: str, done: int, total: int) -> None:
        steps.update("prices", label=f"Pricing: {label}", completed=done, total=total or None)

    engine = PriceEngine(backend, opts, progress=progress)
    priced = engine.price(marketable)
    s = engine.stats
    bits = []
    if s.steam_live or s.steam_cached:
        bits.append(f"Steam {s.steam_live} live/{s.steam_cached} cached")
    if s.skinport_apps:
        bits.append("Skinport")
    if s.venue_apps:
        from stashcard.markets import VENUES

        bits += [VENUES[v].name for v in s.venue_apps]
    if s.csfloat_lookups:
        bits.append(f"CSFloat x{s.csfloat_lookups}")
    steps.done(
        "prices", "Priced: " + (" · ".join(bits) if bits else f"{len(priced)} stacks"), ok=not s.steam_rate_limited
    )

    report = build_report(
        profile,
        opts,
        priced,
        nonmarketable,
        games,
        notices + engine.notices,
        is_demo=getattr(backend, "is_demo", False),
        generated_at=getattr(backend, "as_of", None),
        label=getattr(backend, "label", None),
        fx=engine.fx if engine.fx is not None and not engine.fx.identity else None,
    )

    prev = previous
    if prev is None and history is not None and not report.is_demo:
        prev = history.latest(profile.steamid64, opts.currency, opts.games, before=report.generated_at)
    if prev is not None and prev.currency == report.currency:
        report.delta = delta_from(prev, report)
    if save_history and history is not None and not report.is_demo and report.items:
        history.save(report)
    return report


def _age(seconds: float) -> str:
    from stashcard.cache import human_age

    return human_age(seconds)
