# Changelog

## 1.0.0 · 2026-11-02

- Card values survive a Steam outage mid-refresh.
- The share card keeps its font after a locale change.
- Faster first scan on large inventories.
- Fixed the total row rounding on locales with comma decimals.


All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.8.27] - 2026-10-29

### Added

- Interactive flow (`stashcard`): paste a profile in any format, pick a mode with the arrow keys,
  adjust games, currency, price source, AVG window, minimum price, tradable-only, theme, redaction,
  output folder and auto-open. Choices are remembered.
- Commands for scripts and CI: `card`, `value`, `diff`, `demo`, `render`, `history`, `config`, `cache`,
  `currencies`, with `--json` and `--csv` exports and documented exit codes.
- Profile parsing for URLs, SteamID64, SteamID2, SteamID3, numeric friend codes, `s.team/p/` invite
  links and vanity names.
- Inventory fetching with pagination, a 10 minute cache, per-host pacing, jittered backoff,
  `Retry-After` support, stale-cache fallback and plain rate-limit messages.
- Pricing contract: LOWEST is the current floor, AVG is the mean of real sales over 24h/7d/30d/90d.
  Every number is labelled with its source and window; fallbacks are marked.
- Sources: Steam Community Market, Skinport public API (whole-app catalogs), optional Steam Web API key
  and optional `steamLoginSecure` cookie for true Steam sales averages.
- Outlier guard: median and max next to AVG, "skewed" badge, opt-in `--trimmed-avg`.
- Valve's exact seller-fee rounding, wallet (Steam) vs cash (Skinport) net.
- Honesty checks: unpriced, not marketable, illiquid, days to sell, wide spread, skewed, trade locks,
  fallbacks and rate limits.
- Cards rendered with Pillow at 2x and downsampled: report card, game card, inventory grid (limit or
  paginate), receipt, then-vs-now and A-vs-B diff cards, README banner.
- Themes: midnight, paper, phosphor. Bundled Inter and JetBrains Mono (OFL).
- Privacy redaction presets (`none`, `light`, `full` with value bands), "me" profile defaults,
  metadata-free PNGs.
- Trust footer on every card (sources, windows, currency, UTC time, counts, snapshot hash), plus
  `card.json` sidecars that re-render offline and `card.alt.txt` alt text.
- Local scan history with a "since last scan" delta.
- Inline terminal previews (kitty, iTerm2/WezTerm, sixel) with a half-block fallback.
- Offline demo: two fictional profiles with real item names and icon hashes, a dated multi-venue price
  snapshot, and a dated "then" snapshot so the diff card renders without network.
- `--plain` output and `NO_COLOR` support.
- Third-party marketplaces: Waxpeer, Market.CSGO (and its Dota 2, TF2 and Rust sister sites), Lis-skins
  and, experimental and opt-in, CSFloat sales history as a second AVG source. Whole-catalog feeds are
  fetched once per game and cached for an hour.
- `--markets`, `--lowest-from steam|best|<venue>`, `--fee venue=rate` and `--csfloat-top`; an
  interactive multi-select for marketplaces.
- `markets` mode (command, interactive menu and card): each venue's floor total, fee, net and coverage,
  the best cash route, and the cheapest buy and best cash sell per item. A "By marketplace" strip on
  the report card and venue subtotals on the receipt.
- Wallet vs cash labelling on every venue number; USD-only venues converted with a dated
  open.er-api.com rate (cached 12 hours) that is printed wherever a converted number appears.
- Every venue's floor and net in `--json` and `--csv` exports.
- Windows support: safe console encoding, preview fallbacks for Windows Terminal and the classic
  console, Windows install notes, and Windows in the CI matrix.
- README GIFs recorded by `scripts/make_gifs.py` (dev-only `gifs` extra: pyte, pexpect).

[Unreleased]: https://github.com/AssassinCreator/steam-account-value-checker/compare/v1.8.27...HEAD
[1.8.27]: https://github.com/AssassinCreator/steam-account-value-checker/releases/tag/v1.8.27
