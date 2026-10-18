# Contributing

Thanks for wanting to help. Stashcard is small on purpose; a few rules keep it that way.

## Ground rules

- **Free only.** No paid APIs, no API keys required for the default path, no telemetry, no
  monetization. Optional user-supplied keys and cookies stay on the user's machine and are never
  printed or logged in full.
- **Honest numbers.** LOWEST is the current floor, AVG is the mean of real sales over a window. Every
  number keeps its source and window. A fallback is always labelled. Missing data is "unpriced", never
  zero.
- **Be kind to every source.** Cache, pace, back off, honour `Retry-After`, send the honest User-Agent.
  Probe a new marketplace live before relying on it; keep undocumented endpoints opt-in.
- **Wallet is not cash.** Steam money and third-party cash are labelled and never summed silently.
- **English** in code, comments, docs and output.
- **Licensing.** Do not copy code or data tables from GPL projects (for example CSGO Trader or
  Augmented Steam). Re-derive data from primary sources.

## Setup

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

The tests never touch the network (a fixture blocks it). Use `httpx.MockTransport` for anything that
would.

## Before you open a pull request

1. `pytest -q` passes.
2. `stashcard demo --mode all --out-dir /tmp/cards --offline` still works.
3. If you touched a card layout, render every theme and look at the PNGs: no overflow, no clipped or
   overlapping text, long names truncated with an ellipsis.
4. If the README gallery changes, regenerate it: `python scripts/make_assets.py`, and for the GIFs
   `pip install -e ".[gifs]"` then `python scripts/make_gifs.py` (needs a POSIX pty).
5. Add a line to `CHANGELOG.md` under "Unreleased".

## Layout

```
src/stashcard/
  profile.py      paste-anything profile parsing
  sources.py      Steam, Skinport, marketplace, FX and Web API calls (cache-first)
  markets.py      venues, seller fees and catalog parsers
  fx.py           dated USD exchange rates for USD-only venues
  net.py          pacing, backoff, Retry-After, honest User-Agent
  inventory.py    assets + descriptions -> stacks
  pricing.py      the LOWEST / AVG contract, fallbacks, flags
  report.py       totals and honesty warnings
  snapshot.py     JSON snapshots, sidecars, local history
  diff.py         then-vs-now and A-vs-B
  money.py        currencies, locale price parsing, Valve fee rounding
  render/         Pillow cards (canvas toolkit, themes, layouts)
  term/           Rich output and inline previews
  demo.py         offline demo backend (data/demo.json)
```
