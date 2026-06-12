---
name: log-food
description: "Log meals to Lose It! from a natural-language prompt by driving the lose-it CLI: search → describe → dry-run → log → verify."
triggers:
  - log food
  - log meals
  - log my breakfast
  - log my lunch
  - log my dinner
  - log my snacks
---

# log-food — drive `lose-it` from a natural-language prompt

Turns a plain-English food log into Lose It! diary entries by driving the [`lose-it` CLI](https://github.com/phitoduck/lose-it). The CLI is reverse-engineered and the protocol can drift — **always refresh first**.

## STEP 0 — Refresh the CLI (mandatory, every invocation)

```bash
uv tool install --reinstall git+https://github.com/phitoduck/lose-it
```

If `uv` isn't installed: `brew install uv` (macOS) or `pipx install uv`. Then:

```bash
loseit --help | head -1   # should print "Usage: loseit [OPTIONS] COMMAND [ARGS]..."
```

If the user has never run `loseit login`, do that once now (they must already be signed into loseit.com in Chrome or Brave):

```bash
loseit login                  # default --browser chrome; or --browser brave
```

### Read the README as your CLI reference

The `lose-it` repo ships a single-file CLI docset at its [`README.md`](https://github.com/phitoduck/lose-it/blob/main/README.md). Fetch it once per session — it's the authoritative reference for every subcommand, flag, output format, the full unit alias list, the JSON/TOON schema, and known quirks. Cheaper than guessing.

```bash
curl -sL https://raw.githubusercontent.com/phitoduck/lose-it/main/README.md
```

Or if a clone is already on disk: read it directly from `~/repos/lose-it/README.md`. Either way, keep its contents in your working context while you log.

---

## STEP 1 — Parse the prompt into per-food entries

Decompose the request into one entry per food. Extract:

| Field | Required? | Examples |
|---|---|---|
| **query** | yes | `"xtreme carb balance tortilla"`, `"avocado"`, `"realgood foods chicken strips"` |
| **meal** | yes (default `snacks`) | `breakfast` / `lunch` / `dinner` / `snacks` |
| **quantity + unit** | yes — see below | `120 g`, `1 each`, `0.5 cup`, `2 tsp`, `1 can`, `1 container` |
| **date** | optional (default today) | "yesterday" → `--date YYYY-MM-DD` |

### Unit selection

The CLI accepts these `--serving-unit` values: `tsp`, `tbsp`, `cup`, `piece`, `each`, `g`, `fl_oz`, `mL`, `bottle`, `can`, `slice`, `serving`, `scoop`, `container`, `pie`. Plus common aliases (`cups`, `grams`, `tablespoon`, `floz`, `milliliter`, …).

Heuristics:
- "120g X", "120 grams of X" → `--serving-amount 120 --serving-unit g`
- "1 X", "one X" where X is discrete → `--servings 1` (use the food's native unit)
- "0.5 cup rice" → `--serving-amount 0.5 --serving-unit cup`
- "1 tsp honey" → `--serving-amount 1 --serving-unit tsp`
- "1 can of Coke" → `--serving-amount 1 --serving-unit can`
- "1 container of Chobani" → `--serving-amount 1 --serving-unit container`
- "a serving of X" → `--servings 1`
- Ambiguous "oz" is **rejected** by the CLI — disambiguate to `fl_oz` (volume) or `g` (weight).

### Bare `oz`
The CLI refuses `--serving-unit oz` because it's ambiguous between weight (~28.35 g) and fluid (~29.57 mL). Ask the user — or default to grams for solids, fl_oz for liquids.

---

## STEP 2 — Find the right Lose It! food entry

The food DB has many crowd-sourced entries per query, often with subtly different per-serving math. Two operations matter:

### `search` — list candidates (use TOON for compactness)

```bash
loseit -o toon search "realgood foods chicken strips"
```

TOON output is ~40-60% fewer tokens than JSON for tabular data. The CLI emits `name`, `brand`, `category`, `food_id` (the 32-char hex food identifier) by default. Pass `-v` if you also want the raw `pk_bytes` array (rarely needed).

### `describe-food` — inspect candidates in one batch (preferred, replaces old probe scripts)

Pick the top 3-8 candidate `food_id` values from search and inspect them in **one batched concurrent fetch**:

```bash
loseit -o toon describe-food <food_id_1> <food_id_2> <food_id_3>
```

`describe-food` returns:
- `primary_serving` → `{ordinal, unit, native_qty_per_serving}` — the food's stored serving size (e.g. `unit: "grams"`, `native_qty_per_serving: 1.0` means "1 serving = 1 gram"; `unit: "serving"`, `qty: 1.0` means "1 serving = 1 generic serving")
- `cross_class_conversion` → `{per_serving_g, per_serving_ml}` — what the CLI uses to translate between weight and volume units for a food
- `nutrients_per_serving` → labeled dict: `{calories, total_fat_g, sat_fat_g, cholesterol_mg, sodium_mg, carb_g, fiber_g, sugar_g, protein_g, serving_weight_g, serving_volume_ml, ...}`

**Pick the right candidate by sanity-checking the labeled values**, not by guessing pick indices. Apply these biases in order:

1. **Bias toward the user's requested unit.** If the user said "120g of avocado", prefer entries that support gram-based logging — i.e. `primary_serving.unit == "grams"` OR `cross_class_conversion.per_serving_g` is populated. Not every avocado entry supports grams: one might only have `primary_serving.unit: "cup"` with no `per_serving_g`, in which case `--serving-amount 120 --serving-unit g` will error. Skim the `describe-food` output and drop candidates that don't support the asked-for unit before you get to the dry-run. Same logic for `mL` / `fl_oz` (look for `per_serving_ml` or volumetric native units), `tsp` / `tbsp` / `cup`, etc.

2. **Bias toward foods the user has logged before.** Run `loseit -o toon diary --date <recent>` for a few recent days (or grep the user's diary history if it's already in context) and check whether any of your candidate `food_id`s appear. A prior log is a strong signal the user already approved that entry — re-use it. But don't be blind to it: if the user explicitly names a **new brand** ("the new Costco chicken strips", "Kirkland greek yogurt — switched from Chobani"), let that override and pick a new entry that matches the new brand. Past usage is a strong prior, not a hard constraint.

3. **Sanity-check calories** against common knowledge per the food's native unit (avocado ~160 cal/100g; cooked chicken breast ~165 cal/100g; tomato soup ~80 cal/cup; honey ~20 cal/tsp). If the candidate's per-serving cal is wildly off, skip it.

4. **Prefer entries with a real manufacturer brand** (`Trader Joe's`, `Kodiak Cakes`, `Kirkland Signature`, …) over entries whose brand is empty, equals a category name, or equals the user's own username — personal-DB entries can carry buggy per-serving math.

This *replaces* the old "14-pick Python probe" workflow. Don't write probe scripts; `describe-food` does it in one call.

---

## STEP 3 — Always dry-run first

```bash
loseit log "realgood foods chicken strips" --food-id <hex> -m lunch \
    --serving-amount 120 --serving-unit g --dry-run
# 🟡 DRY RUN — would log Lightly Breaded Chicken Strips (id 4465…) → lunch 120 g (143 cal)
```

The dry-run computes calories using the food's stored per-serving data and the unit/quantity you passed. **Compare to a real-label or USDA expectation**; if the number is off by more than ~10%, you probably picked the wrong entry. Re-run `describe-food` to confirm.

Prefer `--food-id <hex>` to lock onto a specific entry — search result *order* can drift; `food_id` is stable.

When the answer looks right, run again without `--dry-run`:
```bash
loseit log ... --serving-amount 120 --serving-unit g
# ✅ Logged Lightly Breaded Chicken Strips (id 4465…) → lunch 120 g (143 cal)
```

---

## STEP 4 — Verify via diary readback (use labeled keys)

```bash
loseit -o json diary
```

Each entry includes labeled keys you can read at a glance:
- `food_name`, `food_brand`, `food_measure_unit` (`"grams"`, `"cup"`, `"serving"`, `"can"`, …), `servings`, `meal_ordinal` (0=breakfast, 1=lunch, 2=dinner, 3=snacks)
- `nutrients_by_label`: `{calories, total_fat_g, sat_fat_g, protein_g, …}` — already named, no ordinal lookup required

Confirm `food_measure_unit` matches the unit you logged in (e.g. `"grams"` for a `--serving-unit g` log) and `nutrients_by_label.calories` matches the dry-run number.

For the most efficient readback into your context: `loseit -o toon diary | head -50`.

---

## STEP 5 — Fix mis-logs

Delete by 1-based pick within a meal:
```bash
loseit diary                                       # see indices
loseit delete --meal snacks --pick 1 --yes        # delete entry #1 from snacks
```

If `loseit delete` returns HTTP 500, treat it as a parser drift; re-run STEP 0 to make sure you have the latest CLI.

---

## Last-resort debugging

In order from preferred to last-resort:

1. **`loseit describe-food <id>`** — almost any "what does this food look like?" question is answerable from labeled per-serving data + cross-class conversion fields. **Try this first.**
2. **`loseit -o toon diary`** — token-efficient diary readback with labeled keys. Use when something doesn't match the dry-run.
3. **`loseit --log-level trace <subcommand>`** — prints the full GWT request/response bodies. Headers and cookies are **suppressed by default** (the `liauth` JWT is a bearer credential), so this is safe to enable. Useful when the parser drops something and you want to inspect the raw wire.
4. **`loseit --log-level trace --log-headers <subcommand>`** — opts cookies + headers into the trace. Treat output as sensitive; don't paste sessions into bug reports without scrubbing. The repo's gitleaks config blocks committed JWTs.
5. **`chrome-mcp-server`** — if the official Lose It! webapp shows something different from the CLI, drive a browser session via the chrome-mcp tools to capture the network requests the webapp sends and diff against the CLI's payloads. Reserve for last-resort because it requires a Chrome window.

If a food consistently logs at the wrong calorie count, capture the food's `describe-food` output and ask the user to spot-check the daily total in the official Lose It! app — per-serving math on user-edited/personal-DB entries can be subtly broken.

---

## Quick recovery checklist

| Symptom | Fix |
|---|---|
| `loseit: command not found` | STEP 0 (uv tool install --reinstall …) |
| `LoseItAuthError: HTTP 401` | `loseit login` (re-imports cookie or opens signin page) |
| `❌ Missing required setting(s)` | `loseit login` |
| Dry-run cal is way off from real label | Re-run `describe-food` on the chosen `food_id` and verify per-serving cal. Try a different candidate. |
| `loseit log` errors with `unit_not_supported` | The food's stored unit class doesn't match `--serving-unit`. Use `describe-food` to see `primary_serving.unit` and either match it or fall back to `--servings N` in the native unit. |
| Diary shows wrong meal/name/cal | Trust the `loseit log` success line written at log-time; the diary parser can occasionally mis-render display fields. If still wrong in the official app, capture wire via `--log-level trace` and inspect. |
| `loseit delete` HTTP 500 | STEP 0, then retry. If still failing, delete via the official Lose It! app or webapp. |

---

## Reference: command surface (after STEP 0)

```text
loseit --help

Commands:
  login         Import the liauth JWT from Chrome or Brave.
  search        Search the LoseIt food database.
  log           Search for a food and log it to a meal.
  diary         List the diary for a given date (default: today).
  describe-food Inspect one or more foods by ID; fetch concurrent.
  delete        Delete a diary entry by meal + index.
  whoami        Print the resolved client configuration.
```

Global flags relevant to this skill:
- `-o text|json|toon` — output format. **Prefer `toon`** for any output that will be piped back into context (40-60% fewer tokens than JSON).
- `--log-level trace` + `--log-headers` — wire-level debugging (see Last-resort above).
- `--dry-run` (on `log` / `delete`) — preview without sending the mutating RPC.
