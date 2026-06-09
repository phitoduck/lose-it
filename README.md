<h1 align="center">lose-it</h1>

<p align="center">
  <img src="docs/diagram.svg" alt="lose-it CLI → Lose It! web API" width="640"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-3776ab?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/uv-package%20manager-de5fe9?style=flat-square&logo=astral&logoColor=white" alt="uv"/>
  <img src="https://img.shields.io/badge/typer-CLI-009485?style=flat-square&logo=typer&logoColor=white" alt="typer"/>
  <img src="https://img.shields.io/badge/httpx-async%20client-1d2d44?style=flat-square&logo=python&logoColor=white" alt="httpx"/>
  <img src="https://img.shields.io/badge/ruff-lint%20%2B%20format-d7ff64?style=flat-square&logo=ruff&logoColor=000000" alt="ruff"/>
  <img src="https://img.shields.io/badge/gitleaks-secret%20scan-f24c4c?style=flat-square&logo=git&logoColor=white" alt="gitleaks"/>
  <img src="https://img.shields.io/badge/tests-18%2F18-26a269?style=flat-square&logo=pytest&logoColor=white" alt="tests"/>
  <img src="https://img.shields.io/badge/license-MIT-2dba4e?style=flat-square" alt="License"/>
</p>

<p align="center">Unofficial Python SDK + CLI for <a href="https://www.loseit.com/"><b>Lose It!</b></a> — log meals, query your diary, and delete entries from the command line.</p>

> ⚠️ **Reverse-engineered & unofficial.** Talks to Lose It!'s private GWT-RPC web endpoints. No official API exists; the protocol is brittle and may break without notice. Not affiliated with Lose It! / FitNow, Inc.

## Try it without installing (`uvx`)

```bash
# Run a one-off command directly from GitHub (no install)
uvx --from git+https://github.com/phitoduck/lose-it lose-it diary

# Or pin to a specific commit / tag
uvx --from git+https://github.com/phitoduck/lose-it@main lose-it search "tortilla"
```

`uvx` pulls the package straight from the `main` branch, builds it in an ephemeral environment, and runs the entrypoint.

## Install from the tip of main

```bash
# Install into a uv-managed tool environment (re-runnable as `lose-it`)
uv tool install git+https://github.com/phitoduck/lose-it

# Or pin to a commit
uv tool install git+https://github.com/phitoduck/lose-it@<sha>

# Upgrade to the latest main later
uv tool upgrade lose-it-utils
```

After install, `lose-it --help` works system-wide.

## Develop

```bash
git clone https://github.com/phitoduck/lose-it
cd lose-it
uv sync
prek install                           # set up pre-commit hooks
uv run pytest                          # mocked unit tests
LOSEIT_RUN_FUNCTIONAL=1 uv run pytest  # incl. real-API CRUD
```

## Configure

The SDK splits config into two clearly separated buckets so an end user never accidentally posts to someone else's diary:

### Required env vars (no defaults; `Config.from_env` raises if absent)

These identify *you* and intentionally have no fallback — silently using a hardcoded user ID would be a footgun.

```bash
export LOSEIT_USER_ID=12345678          # "sub" claim of your liauth JWT (decode at jwt.io)
export LOSEIT_USER_NAME=your.username   # loseit.com username
export LOSEIT_HOURS_FROM_GMT=-6         # your local offset from UTC
```

### Optional env vars (have defaults, but refresh when LoseIt redeploys)

```bash
export LOSEIT_POLICY_HASH=...    # 5th '|'-field of any /web/service POST body
export LOSEIT_STRONG_NAME=...    # x-gwt-permutation request header
```

### Not user-specific (and not secrets)

The `Class/<digits>` strings you'll see in the SDK source — `UserId/4281239478`, `ServiceRequestToken/1076571655`, `FoodIdentifier/2763145970`, … — are **GWT type-serialization hashes** computed by GWT at compile time from each Java class's structure. They're the same for every user of the same LoseIt build and are inlined in the public `*.cache.js` bundle on `d3hsih69yn4d89.cloudfront.net`. They're protocol type tags, not user/session/account identifiers.

Plus the `liauth` JWT in `~/.config/loseit/token` (or `$LOSEIT_TOKEN`):

```bash
# 1. Log into loseit.com in any browser
# 2. DevTools → Application → Cookies → www.loseit.com → liauth → copy value
mkdir -p ~/.config/loseit
echo "<paste JWT here>" > ~/.config/loseit/token
chmod 600 ~/.config/loseit/token
```

For a more sustainable setup, `lose_it_utils.client.auth.refresh_token_from_chrome()` reads the cookie out of Chrome's encrypted store via `browser-cookie3` (triggers a one-time macOS Keychain prompt; then it's silent).

When LoseIt redeploys, requests start failing with `LoseItError("…IncompatibleRemoteServiceException…")`. Refresh `LOSEIT_POLICY_HASH` / `LOSEIT_STRONG_NAME` from any `/web/service` POST in DevTools — `STRONG_NAME` is the `x-gwt-permutation` header, `POLICY_HASH` is the 5th `|`-separated field of the request body.

## CLI

```text
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

### Example: `search`

```text
$ lose-it search "x-treme carb balance tortilla"

  #  Food                                               Brand
───  ────────────────────────────────────────────────── ────────────────────
  1  Xtreme Wellness Tortilla Wrap High Fiber Low Carb  Carb balance
  2  Tortilla Wraps, High Fiber, Low Carb, Xtreme Welln Mission Tortillas Ca
```

### Example: `log`

```text
$ lose-it log "x-treme carb balance tortilla" --meal lunch --pick 2 --servings 1

  #  Food                                               Brand
───  ────────────────────────────────────────────────── ────────────────────
  1  Xtreme Wellness Tortilla Wrap High Fiber Low Carb  Carb balance
  2  Tortilla Wraps, High Fiber, Low Carb, Xtreme Welln Mission Tortillas Ca

✅ Logged Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness → lunch × 1.0
```

### Example: `diary`

```text
$ lose-it diary

📅 Diary for 2026-06-08:

  Lunch:
    1. Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness (Mission)  × 1.0  [70 cal]
    2. Avocado, whole (Ocado)                                           × 0.55 [177 cal]
    3. Real Good Lightly Breaded Chicken Strips (Real Good Foods)       × 1.43 [186 cal]

  Snacks:
    1. Greek Yogurt, Strawberry, Non Fat (Chobani)                      × 1.0  [110 cal]
```

### Example: `delete`

```text
$ lose-it delete --meal lunch --pick 1 --yes

🗑️  Deleting from lunch: Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness (Mission) × 1.0
✅ Deleted
```

### Example: `whoami`

```text
$ lose-it whoami

user_id        : 12345678
user_name      : your.username
hours_from_gmt : -6
policy_hash    : 8F87EC8969F17AE77B6283D3A83F6D4C
strong_name    : 351AE5DC0CA36AD3BA9C7CBA7B0E07B8
```

### Script-friendly output: `--output json` / `-o json`

Every subcommand accepts a global `--output` (alias `-o`) flag. The default is `text`; pass `json` to get a JSON document on stdout suitable for piping into `jq` or a script.

```text
$ lose-it -o json whoami
{
  "user_id": "12345678",
  "user_name": "your.username",
  "hours_from_gmt": -6,
  "policy_hash": "8F87EC8969F17AE77B6283D3A83F6D4C",
  "strong_name": "351AE5DC0CA36AD3BA9C7CBA7B0E07B8"
}

$ lose-it -o json diary --date 2026-06-08 | jq '.entries[] | .food_name'
"Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness"
"Avocado, whole"
"Real Good Lightly Breaded Chicken Strips"
```

### Preview without mutating: `--dry-run`

`log` and `delete` both accept `--dry-run`. Read-only lookups still run (so you see what *would* be logged or deleted), but the mutating GWT-RPC call is skipped.

```text
$ lose-it log "x-treme carb balance tortilla" -m lunch --pick 2 --dry-run

🟡 DRY RUN — would log Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness → lunch × 1.0 (70 cal)

$ lose-it -o json delete --meal snacks --pick 1 --dry-run
{
  "action": "delete",
  "dry_run": true,
  "date": "2026-06-08",
  "meal": "snacks",
  "target": {
    "food_name": "Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness",
    "food_brand": "...",
    "servings": 1.0,
    ...
  }
}
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

```text
src/lose_it_utils/
├── __init__.py             # exports `Client`
├── cli.py                  # typer CLI
└── client/
    ├── __init__.py         # `Client` class
    ├── _config.py          # Config dataclass + LOSEIT_* env reading
    ├── _http.py            # httpx wrapper + error types
    ├── _gwt.py             # GWT-RPC serialization primitives
    ├── _models.py          # FoodSearchResult / UnsavedFoodLogEntry / FoodLogEntry
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

The functional suite is the *source of truth* for the mock fixtures: each CRUD step writes the raw response body to `tests/conformance/fixtures/` (after redacting user_id / username). The unit tests then replay those fixtures through `pytest-httpx` mocks, so the mocked request/response shapes are guaranteed to match what the real Lose It! servers actually emit.

## Lint, format, secret-scan (prek)

`prek` (the Rust pre-commit drop-in) runs three classes of checks on every commit:

```bash
prek install            # one-time: wire the git hook
prek run --all-files    # run everything explicitly
```

Hooks (declared in `.pre-commit-config.yaml`):

| Hook | What it does |
|---|---|
| `pre-commit-hooks` | trailing whitespace, EOL, merge conflicts, large files, YAML/TOML syntax, `detect-private-key` |
| `ruff-check --fix` | lint + auto-fix Python with the ruleset in `pyproject.toml` |
| `ruff-format` | format all `.py` files (PEP 8, 100-char lines) |
| `gitleaks` | scan the staged diff for secrets, with a `.gitleaks.toml` rule for LoseIt's exact `liauth` JWT shape (`ES384` + `kid=MD2BMUN8VL`) |

The gitleaks allowlist permits the sanitized JWT placeholders in `tests/conformance/fixtures/`. Everything else — real tokens, AWS keys, GitHub PATs, generic high-entropy strings — gets caught.

## Known quirks (annotated in the parser)

- **GWT writes byte arrays in reverse**: both PKs you see in responses are reversed copies of their wire-form bytes. `_gwt.reverse_bytes` handles round-trips.
- **GWT writes object fields in declaration order, dedup'd across an array**: when several `FoodLogEntry` objects share the same enum value (e.g. all in *snacks*) or the same nutrient HashMap (e.g. multiple identical logs), the response writes the shared value *once* and references it from each entry. The parser falls back to a global search when an entry's local range comes up empty.
- **Food codes can contain `$` and `_`**: e.g. `DoA3$q`. The food-identifier-code regex allows the full GWT short-string alphabet.
- **The serving-unit is the food's default**: passing `--servings 1.1` to a per-100g entry logs 110 g, but to a "1 Each" entry it logs 1.1 each. There's no "log in grams" override yet — pick the right base food.

## License

MIT.
