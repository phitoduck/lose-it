<h1 align="center">lose-it</h1>

<p align="center">Unofficial Python SDK + CLI for <a href="https://www.loseit.com/"><b>Lose It!</b></a> — log meals, query your diary, and delete entries from the command line.</p>

<p align="center">
  <img src="docs/diagram.svg" alt="lose-it CLI → Lose It! web API" width="640"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-3776ab?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"/>
  <img src="https://github.com/phitoduck/lose-it/actions/workflows/ci.yml/badge.svg" alt="CI"/>
  <img src="https://img.shields.io/badge/coverage-75%25-yellowgreen?style=flat-square&logo=pytest&logoColor=white" alt="coverage 75%"/>
  <img src="https://img.shields.io/badge/license-MIT-2dba4e?style=flat-square" alt="License"/>
</p>

> ⚠️ **Reverse-engineered & unofficial.** Talks to Lose It!'s private GWT-RPC web endpoints. No official API exists; the protocol is brittle and may break without notice. Not affiliated with Lose It! / FitNow, Inc.

## Quickstart

You need to already be signed into [loseit.com](https://www.loseit.com/) in Chrome or Brave.

```bash
# 1. Install the CLI (system-wide via uv)
uv tool install git+https://github.com/phitoduck/lose-it

# 2. Import your auth token AND populate the config from the browser
lose-it login                       # default: --browser chrome
# or:  lose-it login --browser brave

# 3. You're ready
lose-it diary
```

Prompt for, e.g. Claude Code:

> Log 1 Xtreme carb balance tortilla, 110g of avocado, and 120g of real good brand lightly breaded chicken strips

<p align="center">
  <img src="docs/mobile-app-demo.jpeg" alt="Lose It! mobile app — Mon, Jun 8: Snacks (451 cal): Tortilla Wraps 1 Serving (70 cal), Real good Lightly Breaded Chicken 120 Grams (187 cal), Avocado (USDA Website Per 100g) 110 Grams (194 cal)" width="320"/>
</p>

`lose-it login` does the one-time setup for you: it imports the `liauth` JWT from the browser, derives `user_id` from the JWT's `sub` claim, picks up `user_name` from the JWT payload or the browser's other `loseit.com` cookies (prompting once if neither has it), reads `hours_from_gmt` from your OS timezone, and writes them all to `~/.config/loseit/config.yaml`. No `LOSEIT_*` env vars to set by hand — see [Configuration](#configuration) for layered overrides.

If `lose-it login` reports the cookie is missing or expired, it opens the Lose It! signin page in the chosen browser — sign in, then re-run `lose-it login`.

Want to skip the install? `uvx --from git+https://github.com/phitoduck/lose-it lose-it diary` runs any command in an ephemeral environment.

## Examples

```text
$ lose-it --help

 Usage: lose-it [OPTIONS] COMMAND [ARGS]...

 Unofficial Lose It! food logger / diary CLI.

╭─ Commands ──────────────────────────────────────────────────────╮
│ login   Import the liauth JWT from Chrome or Brave.             │
│ search  Search the LoseIt food database.                        │
│ log     Search for a food and log it to a meal.                 │
│ diary   List the diary for a given date (default: today).       │
│ delete  Delete a diary entry by meal + index.                   │
│ whoami  Print the resolved client configuration.                │
╰─────────────────────────────────────────────────────────────────╯
```

### `login` — import the auth token *and* populate the config

```text
$ lose-it login --browser chrome
✅ Imported liauth from Chrome → /Users/you/.config/loseit/token
   JWT exp: 2026-06-22T20:41:44+00:00
✅ Wrote config → /Users/you/.config/loseit/config.yaml
   user_name     : you@example.com
   hours_from_gmt: -6
   user_id       : 12345678

$ lose-it login --browser brave
❌ liauth cookie in Brave is expired.
   JWT exp: 2026-04-01T12:00:00+00:00 (now: 2026-06-08T19:30:00+00:00)
   Opened https://www.loseit.com/ in Brave.
   Then re-run: lose-it login --browser brave
```

The first run on macOS triggers a Keychain prompt so the OS can unlock the browser's cookie store. After that it's silent. If neither the JWT nor any `loseit.com` cookie carries your username, `lose-it login` prompts once and saves it to the YAML. Pass `--user-name alice@example.com` to skip the prompt (handy in CI), or `--no-write-config` to import only the token.

### `search`

```text
$ lose-it search "x-treme carb balance tortilla"

  #  Food                                               Brand                Food ID
───  ────────────────────────────────────────────────── ──────────────────── ───────────
  1  Xtreme Wellness Tortilla Wrap High Fiber Low Carb  Carb balance         9eba9129b8…
  2  Tortilla Wraps, High Fiber, Low Carb, Xtreme Welln Mission Tortillas Ca 0d5f30bf42…
```

The `Food ID` column is the lowercase-hex form of the food's stable 16-byte primary key. The text view truncates it to 10 chars; the full 32-char value is in the JSON `food_id` field (`lose-it search ... -o json`).

### `log`

```text
$ lose-it log "x-treme carb balance tortilla" --meal lunch --pick 2 --servings 1

  #  Food                                               Brand                Food ID
───  ────────────────────────────────────────────────── ──────────────────── ───────────
  1  Xtreme Wellness Tortilla Wrap High Fiber Low Carb  Carb balance         9eba9129b8…
  2  Tortilla Wraps, High Fiber, Low Carb, Xtreme Welln Mission Tortillas Ca 0d5f30bf42…

✅ Logged Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness (id 0d5f…) → lunch 1 serving (70 cal)
```

For foods that should be logged by weight, use `--grams N` on a gram-measured entry. The CLI validates the picked food and errors if it isn't gram-measured, so you don't accidentally log "1.2 grams of chicken":

```text
$ lose-it log "avocado per 100g" --meal snacks --pick 8 --grams 110
✅ Logged Avocado (USDA Website Per 100g) (id 7f3a…) → snacks 110 grams (176 cal)
```

Pick indices drift between sessions (Lose It! mutates its index). For drift-proof, scriptable logging, pass the stable `--food-id` instead — it skips the search step entirely:

```text
$ lose-it log --food-id 0d5f30bf4231a3da8c3c9608f0630010 --meal snacks --servings 1.0
✅ Logged Organic Tomato And Roasted Red Pepper Soup (id 0d5f…) → snacks 1 serving (207 cal)
```

`--food-id` is mutually exclusive with the positional `<query>` and `--pick`; pass exactly one.

### `diary`

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

### `delete`

```text
$ lose-it delete --meal lunch --pick 1 --yes

🗑️  Deleting from lunch: Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness (Mission) × 1.0
✅ Deleted
```

### `whoami`

```text
$ lose-it whoami

user_id        : 12345678
user_name      : your.username
hours_from_gmt : -6
policy_hash    : 8F87EC8969F17AE77B6283D3A83F6D4C
strong_name    : 351AE5DC0CA36AD3BA9C7CBA7B0E07B8
```

### Script-friendly output: `--output json` / `-o json`

Every subcommand accepts a global `--output` (alias `-o`) flag. The default is `text`; pass `json` to get a JSON document on stdout suitable for piping into `jq`.

```text
$ lose-it -o json diary --date 2026-06-08 | jq '.entries[] | .food_name'
"Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness"
"Avocado, whole"
"Real Good Lightly Breaded Chicken Strips"
```

### Preview without mutating: `--dry-run`

`log` and `delete` accept `--dry-run`. Read-only lookups still run (so you see what *would* be logged or deleted), but the mutating RPC is skipped.

```text
$ lose-it log "x-treme carb balance tortilla" -m lunch --pick 2 --dry-run

🟡 DRY RUN — would log Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness → lunch × 1.0 (70 cal)
```

### Verbose logging: `--log-level` / `--log-file`

Every subcommand accepts two global logging flags (powered by [loguru](https://github.com/Delgan/loguru)):

- `--log-level {trace|debug|info|success|warning|error|critical}` (env: `LOSEIT_LOG_LEVEL`) — verbosity for logs emitted on **stderr**. Default: **muted** (the CLI is silent unless you opt in).
- `--log-file PATH` (env: `LOSEIT_LOG_FILE`) — append a **full TRACE-level transcript** of the session to this file. The file sink always captures at TRACE regardless of `--log-level`, so you can keep the console quiet and still get the wire dump on disk.

The levels, from loudest to quietest:

| Level | What you get |
|---|---|
| `trace` | Full HTTP request + response dumps: URL, every header, both auth cookies, full GWT-RPC body for each direction. |
| `debug` | One-liner per RPC (`rpc <method>: POST <url> → <status> in Xms`), payload sizes, parser intermediates. |
| `info` | High-level events: CLI command + args, search queries, food lookups, log/delete intent, resolved user. |
| `success` | Mutating RPCs that returned `//OK`. |
| `warning` | Degraded paths (empty diaries, missing day-keys, fallback parses). |
| `error` | HTTP 4xx/5xx, GWT `//EX[…]` errors, transport failures. |

**`--log-level info`** — one line per high-level event:

```text
$ lose-it --log-level info search "tortilla" 1>/dev/null
12:00:00.123 INFO     lose_it_utils.cli:search:344 cli.search: query='tortilla' output=text
12:00:00.135 INFO     lose_it_utils.client:from_env:65 Client.from_env: user='you@example.com' hours_from_gmt=-6 permutation=351AE5DC0CA36AD3BA9C7CBA7B0E07B8
12:00:00.140 INFO     lose_it_utils.client.foods:search:135 foods.search: query='tortilla'
12:00:00.330 SUCCESS  lose_it_utils.client._http:post_rpc:205 rpc searchFoods OK in 188.5 ms (2764 bytes)
```

**`--log-level trace`** — dumps every request and response in full, headers included. Useful for reverse-engineering the GWT-RPC surface:

```text
$ lose-it --log-level trace search "avocado" 1>/dev/null
12:00:00.123 TRACE    lose_it_utils.client._http:post_rpc:121 HTTP REQUEST → searchFoods
POST https://www.loseit.com/web/service
── headers ──
  content-type: text/x-gwt-rpc; charset=UTF-8
  x-gwt-module-base: https://d3hsih69yn4d89.cloudfront.net/web/
  x-gwt-permutation: 351AE5DC0CA36AD3BA9C7CBA7B0E07B8
  x-loseit-gwtversion: devmode
  x-loseit-hoursfromgmt: -6
  origin: https://www.loseit.com
  referer: https://www.loseit.com/
── cookies ──
  liauth=<REDACTED-JWT>
  fn_auth=<REDACTED-JWT>
── body (380 bytes) ──
7|0|12|https://d3hsih69yn4d89.cloudfront.net/web/|8F87EC8969F17AE77B6283D3A83F6D4C|com.loseit.core.client.service.LoseItRemoteService|searchFoods|com.loseit.core.client.service.ServiceRequestToken/1076571655|java.lang.String/2004016611|I|Z|com.loseit.core.client.model.UserId/4281239478|you@example.com|avocado|en-US|1|2|3|4|6|5|6|6|7|8|8|5|0|9|12345678|10|-6|11|12|15|1|1|
12:00:00.310 TRACE    lose_it_utils.client._http:post_rpc:150 HTTP RESPONSE ← searchFoods
HTTP/1.1 200 (188.4 ms)
── headers ──
  content-type: application/json;charset=utf-8
  content-length: 1235
  ...
── body (2764 bytes) ──
//OK[0,17,2,42,40,13,-51,-6,9290,"Z6mB_lo",...]
```

**`--log-file`** — captures a full session to disk (TRACE-level by default), regardless of `--log-level`. The file format uses pipe-separated columns so it round-trips cleanly through `less` / `grep`:

```text
$ lose-it --log-file ~/lose-it-session.log diary
$ grep -E "rpc.*OK" ~/lose-it-session.log
2026-06-11 12:00:00.578 | SUCCESS  | lose_it_utils.client._http:post_rpc:205 | rpc getInitializationData OK in 159.1 ms (10306 bytes)
2026-06-11 12:00:00.672 | SUCCESS  | lose_it_utils.client._http:post_rpc:205 | rpc getDailyDetailsIncludingPendingForDate OK in 92.7 ms (7610 bytes)
```

> ⚠️ **Heads up:** at TRACE level the `liauth` and `fn_auth` cookies are dumped **verbatim** — the JWT is a bearer credential for your Lose It! account. Treat any file written via `--log-file` as sensitive: don't paste sessions into bug reports without scrubbing them. The repo's gitleaks config (see [Lint, format, secret-scan (prek)](#lint-format-secret-scan-prek)) blocks any commit containing a real `liauth`/`fn_auth` JWT exactly to prevent accidental disclosure.

## Configuration

The Quickstart used env vars because they're the fastest path. For anything more permanent, every setting can also come from a YAML file or a CLI flag.

### Priority (highest wins)

1. **CLI flag** — e.g. `lose-it --user-id 12345678 whoami`
2. **`LOSEIT_*` env var** — e.g. `LOSEIT_USER_ID=…`
3. **YAML file** — default path `~/.config/loseit/config.yaml`
   (override with `--config-file` or `LOSEIT_CONFIG_FILE`)
4. **Built-in default** — applied when no other layer sets the field

`user_id`, `user_name`, and `hours_from_gmt` have **no defaults** — the SDK raises `MissingConfigError` rather than silently posting to the wrong account. `lose-it login` writes all three to the YAML on first run, so you only hit this error if you skipped the login flow (e.g. running in CI).

### YAML file (most ergonomic for long-term setup)

```yaml
# ~/.config/loseit/config.yaml — every key matches a field in the Settings model.
user_id: "12345678"
user_name: your.username
hours_from_gmt: -6

# Optional — refresh from any /web/service POST in DevTools when requests
# start failing with IncompatibleRemoteServiceException.
# policy_hash: 8F87EC8969F17AE77B6283D3A83F6D4C
# strong_name: 351AE5DC0CA36AD3BA9C7CBA7B0E07B8
```

### All settings

| YAML key / field  | CLI flag             | Env var                 | Type   | Default                                  | Description                                                                 |
|-------------------|----------------------|-------------------------|--------|------------------------------------------|-----------------------------------------------------------------------------|
| `user_id`         | `--user-id`          | `LOSEIT_USER_ID`        | `str`  | *(written by `lose-it login`)*           | Numeric `sub` claim of your `liauth` JWT — `lose-it login` extracts it for you.            |
| `user_name`       | `--user-name`        | `LOSEIT_USER_NAME`      | `str`  | *(written by `lose-it login`)*           | Your loseit.com username — `lose-it login` sniffs it from the JWT/cookies or prompts once. |
| `hours_from_gmt`  | `--hours-from-gmt`   | `LOSEIT_HOURS_FROM_GMT` | `int`  | *(written by `lose-it login`)*           | Local offset from UTC (e.g. `-6`) — `lose-it login` reads it from the OS timezone.         |
| `policy_hash`     | `--policy-hash`      | `LOSEIT_POLICY_HASH`    | `str`  | last-known-good                          | 5th `\|`-field of any `/web/service` POST body. Refresh on LoseIt redeploy. |
| `strong_name`     | `--strong-name`      | `LOSEIT_STRONG_NAME`    | `str`  | last-known-good                          | `x-gwt-permutation` request header. Refresh on LoseIt redeploy.             |
| `base_url`        | *(not exposed)*      | `LOSEIT_BASE_URL`       | `str`  | `https://d3hsih69yn4d89.cloudfront.net/web/` | GWT module base URL.                                                    |
| `service_url`     | *(not exposed)*      | `LOSEIT_SERVICE_URL`    | `str`  | `https://www.loseit.com/web/service`     | GWT-RPC service endpoint.                                                   |
| `token`           | *(not exposed)*      | `LOSEIT_TOKEN`          | `str`  | `None` → read from `token_file`          | `liauth` JWT. If unset, falls back to reading `token_file`.                 |
| `token_file`      | *(not exposed)*      | `LOSEIT_TOKEN_FILE`     | `Path` | `~/.config/loseit/token`                 | Where `lose-it login` writes the JWT, and where the SDK reads it from.      |

The pydantic-settings model in [`src/lose_it_utils/client/_settings.py`](src/lose_it_utils/client/_settings.py) is the single source of truth and the spec of the YAML file.

### Refreshing the auth token

`lose-it login` is the easy path; it reads the cookie out of Chrome or Brave (via `browser-cookie3`) and writes it to `~/.config/loseit/token`. The manual fallback:

```bash
# DevTools → Application → Cookies → www.loseit.com → liauth → copy value
mkdir -p ~/.config/loseit
echo "<paste JWT here>" > ~/.config/loseit/token
chmod 600 ~/.config/loseit/token
```

You can also set `LOSEIT_TOKEN=<jwt>` directly.

### Refreshing `policy_hash` / `strong_name`

When LoseIt redeploys, requests start failing with `LoseItError("…IncompatibleRemoteServiceException…")`. Open DevTools, find any `/web/service` POST:

- `strong_name` = the `x-gwt-permutation` request header
- `policy_hash` = the 5th `|`-separated field of the request body

### Not user-specific (and not secrets)

The `Class/<digits>` strings you'll see in the SDK source — `UserId/4281239478`, `ServiceRequestToken/1076571655`, `FoodIdentifier/2763145970`, … — are **GWT type-serialization hashes** computed by GWT at compile time from each Java class's structure. They're the same for every user of the same LoseIt build and are inlined in the public `*.cache.js` bundle on `d3hsih69yn4d89.cloudfront.net`. They're protocol type tags, not user/session/account identifiers.

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

## Develop

```bash
git clone https://github.com/phitoduck/lose-it
cd lose-it
uv sync
prek install                           # set up pre-commit hooks
uv run pytest                          # mocked unit tests
LOSEIT_RUN_FUNCTIONAL=1 uv run pytest  # incl. real-API CRUD
```

### Package layout

```text
src/lose_it_utils/
├── __init__.py             # exports `Client`
├── cli.py                  # typer CLI
└── client/
    ├── __init__.py         # `Client` class
    ├── _settings.py        # pydantic-settings layered config (CLI > env > YAML > defaults)
    ├── _config.py          # backwards-compat `Config` façade over Settings
    ├── _http.py            # httpx wrapper + error types
    ├── _gwt.py             # GWT-RPC serialization primitives
    ├── _models.py          # FoodSearchResult / UnsavedFoodLogEntry / FoodLogEntry
    ├── _dates.py           # date ↔ day-number conversion
    ├── auth.py             # token loading + Chrome/Brave cookie import
    ├── init.py             # getInitializationData → DayDate key lookup
    ├── foods.py            # searchFoods + getUnsavedFoodLogEntry
    ├── entries.py          # updateFoodLogEntry (log) + deleteFoodLogEntry
    └── daily.py            # getDailyDetailsIncludingPendingForDate
```

Module structure mirrors the underlying GWT-RPC resources: one module per backend resource, one function per RPC method.

### Tests

```bash
# Unit tests (mocked httpx, replay captured fixtures) + coverage
uv run pytest --cov=lose_it_utils --cov-report=term-missing

# Real-API CRUD (requires LOSEIT_RUN_FUNCTIONAL=1 + valid token + config)
LOSEIT_RUN_FUNCTIONAL=1 uv run pytest tests/functional
```

GitHub Actions runs the unit suite + coverage on every push/PR to `main` (Python 3.12, ubuntu-latest); the functional suite is gated on `LOSEIT_RUN_FUNCTIONAL=1` and is **not** run in CI because it needs a real `liauth` JWT on disk.

The functional suite is the *source of truth* for the mock fixtures: each CRUD step writes the raw response body to `tests/conformance/fixtures/` (after redacting user_id / username). The unit tests then replay those fixtures through `pytest-httpx` mocks, so the mocked request/response shapes are guaranteed to match what the real Lose It! servers actually emit.

### Lint, format, secret-scan (prek)

`prek` (the Rust pre-commit drop-in) runs three classes of checks at commit *and* push time:

```bash
prek install            # one-time: wire the git hook
prek run --all-files    # run everything explicitly
```

| Hook | What it does |
|---|---|
| `pre-commit-hooks` | trailing whitespace, EOL, merge conflicts, large files, YAML/TOML syntax, `detect-private-key` |
| `ruff-check --fix` | lint + auto-fix Python with the ruleset in `pyproject.toml` |
| `ruff-format` | format all `.py` files (PEP 8, 100-char lines) |
| `gitleaks` | scan for secrets (default ruleset + custom Lose It! JWT rules); runs at both `pre-commit` and `pre-push` |

The gitleaks config has two custom rules: a tight ES384-JWT match (kid-agnostic, so signing-key rotations still trip it) and a fallback that catches any `liauth`/`fn_auth` cookie name beside a JWT-shaped value regardless of algorithm. The sanitized JWT placeholders in `tests/conformance/fixtures/` are allowlisted; everything else gets caught.

## Known quirks (annotated in the parser)

- **GWT writes byte arrays in reverse**: both PKs you see in responses are reversed copies of their wire-form bytes. `_gwt.reverse_bytes` handles round-trips.
- **GWT writes object fields in declaration order, dedup'd across an array**: when several `FoodLogEntry` objects share the same enum value (e.g. all in *snacks*) or the same nutrient HashMap (e.g. multiple identical logs), the response writes the shared value *once* and references it from each entry. The parser falls back to a global search when an entry's local range comes up empty.
- **Food codes can contain `$` and `_`**: e.g. `DoA3$q`. The food-identifier-code regex allows the full GWT short-string alphabet.
- **The serving-unit is the food's default**: `--servings N` is a multiplier on the food's default serving — 1 wrap, 1 avocado, 100 g, etc. For **gram-measured** foods (FoodMeasurement ordinal 8) the default serving is 100 g, so `--servings 1.2` correctly logs 120 g (the official UI displays "120 grams"). For more natural gram logging, use `--grams N` on a gram-measured food entry: `lose-it log "avocado per 100g" --pick 8 -m snacks --grams 110` logs 110 g directly and errors if the picked entry isn't gram-measured. The CLI advertises the food's unit in the dry-run / log output (`× 110 grams`, `× 1.0 serving`, `× 1.1 each`).

## License

MIT.
