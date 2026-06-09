# lose-it

[![Tests](https://img.shields.io/badge/tests-18%2F18-brightgreen)](#tests)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)

**Unofficial Python SDK and CLI for [Lose It!](https://www.loseit.com/) — log meals, query your diary, and delete entries from the command line.**

> ⚠️ **Reverse-engineered & unofficial.** Talks to Lose It!'s private GWT-RPC web endpoints. No official API exists; the protocol is brittle and may break without notice. Not affiliated with Lose It! / FitNow, Inc.

## Highlights

- **SDK first**: ``lose_it_utils.Client`` + per-resource modules (``client.foods``, ``client.entries``, ``client.daily``, ``client.init``). Each RPC is a single function — no implicit state.
- **CLI**: ``lose-it`` (typer) with subcommands ``search``, ``log``, ``diary``, ``delete``, ``whoami``.
- **httpx** under the hood. No ``requests`` dependency.
- **Real-API + mocked-unit test coverage**: a single functional CRUD test exercises the live API and saves the raw GWT-RPC responses as fixtures; unit tests then mock httpx and replay those fixtures, so the mocks are guaranteed to match production wire shapes.
- **Sanitized fixtures**: account-identifying values are scrubbed before being committed, so the captured request/response bodies are safe to publish.

## Install

Requires Python 3.12+.

```bash
git clone https://github.com/phitoduck/lose-it
cd lose-it
uv sync
```

## Configure

The SDK reads everything from environment variables (and a token file). All keys are optional but the defaults point to the original reverse-engineer's account, so you'll want to set the user-specific ones:

```bash
# Per-account (stable)
export LOSEIT_USER_ID=12345678          # "sub" claim of your liauth JWT
export LOSEIT_USER_NAME=your.username   # loseit.com username
export LOSEIT_HOURS_FROM_GMT=-6         # your local offset from UTC

# Per-build (refresh whenever LoseIt redeploys their web app)
export LOSEIT_POLICY_HASH=8F87EC8969F17AE77B6283D3A83F6D4C
export LOSEIT_STRONG_NAME=351AE5DC0CA36AD3BA9C7CBA7B0E07B8
```

Plus a JWT in ``~/.config/loseit/token`` (or ``$LOSEIT_TOKEN``):

```bash
# Manual capture (lasts ~2 weeks):
# 1. Log into loseit.com in any browser
# 2. DevTools → Application → Cookies → www.loseit.com → liauth → copy value
mkdir -p ~/.config/loseit
echo "<paste JWT here>" > ~/.config/loseit/token
chmod 600 ~/.config/loseit/token
```

For a more sustainable setup, ``lose_it_utils.client.auth.refresh_token_from_chrome()`` reads the cookie out of Chrome's encrypted store via ``browser-cookie3`` (triggers a one-time macOS Keychain prompt; then it's silent).

When LoseIt redeploys, requests start failing with ``LoseItError("…IncompatibleRemoteServiceException…")``. Refresh ``LOSEIT_POLICY_HASH`` / ``LOSEIT_STRONG_NAME`` from any ``/web/service`` POST in DevTools — ``STRONG_NAME`` is the ``x-gwt-permutation`` header, ``POLICY_HASH`` is the 5th ``|``-separated field of the request body.

## CLI

```
$ lose-it --help

 Usage: lose-it [OPTIONS] COMMAND [ARGS]...

 Unofficial Lose It! food logger / diary CLI.

╭─ Commands ──────────────────────────────────────────────────────╮
│ search  Search the LoseIt food database.                        │
│ log     Search for a food and log it to a meal.                 │
│ diary   List the diary for a given date (default: today).       │
│ delete  Delete a diary entry by meal + index.                   │
│ whoami  Print the resolved client configuration.                │
╰─────────────────────────────────────────────────────────────────╯
```

### Examples

```bash
lose-it search "x-treme carb balance tortilla"

lose-it log "x-treme carb balance tortilla" --meal lunch --pick 2 --servings 1

lose-it diary
lose-it diary --date 2026-06-05

lose-it delete --meal lunch --pick 1 --yes
```

## SDK

```python
from datetime import date
from lose_it_utils import Client
from lose_it_utils.client import foods, entries, daily
from lose_it_utils.client._dates import day_number_for
from lose_it_utils.client.init import get_daydate_key

with Client.from_env() as client:
    # Search
    results = foods.search(client.http, "tortilla")
    chosen = results[0]

    # Get the food's nutrient template, then log 1 serving to lunch
    unsaved = foods.get_unsaved_food_log_entry(client.http, chosen)
    day_num = day_number_for(date.today())
    day_key = get_daydate_key(client.http, day_num)
    entries.log_food(client.http, unsaved, meal_ordinal=1,
                     day_key=day_key, day_num=day_num, servings=1.0)

    # List + delete
    for e in daily.get_daily_details(client.http, date.today()):
        print(f"{e.food_name}  × {e.servings}  [{e.calories} cal]")
        if "tortilla" in e.food_name.lower():
            entries.delete(client.http, e)
```

## Package layout

```
src/lose_it_utils/
├── __init__.py             # exports `Client`
├── cli.py                  # typer-based CLI
└── client/
    ├── __init__.py         # `Client` class
    ├── _config.py          # Config dataclass + LOSEIT_* env reading
    ├── _http.py            # httpx wrapper + error types
    ├── _gwt.py             # GWT-RPC serialization primitives
    ├── _models.py          # dataclasses (FoodSearchResult, UnsavedFoodLogEntry, FoodLogEntry)
    ├── _dates.py           # date ↔ day-number conversion
    ├── auth.py             # token loading + Chrome cookie refresh
    ├── init.py             # getInitializationData → DayDate key lookup
    ├── foods.py            # searchFoods + getUnsavedFoodLogEntry
    ├── entries.py          # updateFoodLogEntry (log) + deleteFoodLogEntry
    └── daily.py            # getDailyDetailsIncludingPendingForDate
```

Module structure mirrors the underlying GWT-RPC resources: one module per backend resource, one function per RPC method.

## Tests

```bash
# Unit tests (mocked httpx, replay captured fixtures)
uv run pytest tests/conformance

# Real-API CRUD (requires LOSEIT_RUN_FUNCTIONAL=1 + valid token + config env vars)
LOSEIT_RUN_FUNCTIONAL=1 uv run pytest tests/functional

# Everything
LOSEIT_RUN_FUNCTIONAL=1 uv run pytest
```

The functional suite is the *source of truth* for the mock fixtures: each CRUD step writes the raw response body to ``tests/conformance/fixtures/`` (after redacting user_id / username). The unit tests then replay those fixtures through ``pytest-httpx`` mocks, so the mocked request/response shapes are guaranteed to match what the real Lose It! servers actually emit.

## Known quirks (annotated in the parser)

- **GWT writes byte arrays in reverse**: both PKs you see in responses are reversed copies of their wire-form bytes. ``_gwt.reverse_bytes`` handles round-trips.
- **GWT writes object fields in declaration order, dedup'd across an array**: when several ``FoodLogEntry`` objects share the same enum value (e.g. all in *snacks*) or the same nutrient HashMap (e.g. multiple identical logs), the response writes the shared value *once* and references it from each entry. The parser falls back to a global search when an entry's local range comes up empty.
- **Food codes can contain ``$`` and ``_``**: e.g. ``DoA3$q``. The food-identifier-code regex allows the full GWT short-string alphabet.
- **The serving-unit is the food's default**: passing ``--servings 1.1`` to a per-100g entry logs 110 g, but to a "1 Each" entry it logs 1.1 each. There's no "log in grams" override yet — pick the right base food.

## License

MIT.
