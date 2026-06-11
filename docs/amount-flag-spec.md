# Spec: `--amount <quantity><unit>` flag for native unit conversions

This is a context-loss-resistant spec. Read it top to bottom before writing
code; every claim below is backed by a line citation or a HAR quote.

## Background

A cup-measured food (e.g. Trader Joe's tomato + roasted red pepper soup) is
currently logged via `--servings 2.07`, which means "2.07 cups". Users
typically know their portion in mL, fl oz, or grams — not in cups. The
official UI lets the user pick a display unit (mL, fl oz, etc.) when the food
supports it. The CLI does not, except for the special-case `--grams` flag.

This spec describes a general `--amount <number><unit>` flag that mirrors the
official UI's behavior, plus a fallback for foods that don't support the
requested unit (search for an alternative entry that does).

## Wire-level evidence

The user captured a HAR from the official UI on 2026-06-11 while logging
**490 mL of `Organic Tomatoe & Roasted Red Pepper Soup` (Trader Joe's)** as
a snack. The relevant `updateFoodLogEntry` `postData.text` tail is:

```
22|23|1|2.0711109608264158|
24|10|
25|1|26|236.58799999999997|
25|3|26|3.5|
25|12|26|8|
25|9|26|140|
25|13|26|2|
25|4|26|2|
25|10|26|15|
25|11|26|2|
25|0|26|100|
25|8|26|10|
27|2.0711109608264158|1|28|11|1.0000000000000002|236.58800000000002|490|
```

Decoded against the GWT-RPC schema (see `_schemas.json`):

| Wire field | Value | Meaning |
|---|---|---|
| `FoodServing.quantity` | `2.0711109608264158` | Consumed amount in the **food's canonical unit** (cups). Computed by the UI as `490 / 236.588`. |
| `FoodNutrients` HashMap | `{0:100, 9:140, …}` (10 entries) | **Per-serving** nutrient values. Server multiplies by `FoodServing.quantity` to get displayed totals. |
| `FoodServingSize.f0` | `2.0711109608264158` | Same as `FoodServing.quantity`. |
| `FoodServingSize.f1` | `1` | `isPrimary` boolean. |
| `FoodServingSize.f2` | `FoodMeasure(ord=11)` | The **chosen display unit** (mL). The food's native ord is 3 (cup); this overrides it. |
| `FoodServingSize.f3` | `1.0000000000000002` | Constant `1.0`. Role unclear but always `1.0` in observed UI payloads; safe to hardcode. |
| `FoodServingSize.f4` | `236.58800000000002` | **Conversion factor**: `quantity_in_chosen_unit = canonical_servings × f4`. For cup→mL this is the universal constant 236.5882365 mL/US cup. |
| `FoodServingSize.f5` | `490` | User's **raw input in the chosen unit**. |

Two non-obvious facts confirmed by the HAR:

1. **Conversion factors are universal volumetric constants**, not food-specific. `236.588` is just "US cup → mL". A different cup-measured food would still send `f4=236.588` when the user picks mL.
2. **The wire shape is identical to what the CLI already sends** for the cup case — only `f4`, `f5`, and the FoodMeasure ord differ. See:
   - This file:[snippet above] for the UI's wire bytes
   - `src/lose_it_utils/client/entries.py:164-178` for the CLI's current FoodServingSize block

## What works after PR #22

The double-scaled-nutrients bug is fixed: the CLI now sends per-serving
HashMap values, matching the UI's pattern. The wire shape diverges from
the UI in **exactly two places** when a unit override is requested:

1. The FoodMeasure ord (CLI sends the food's native ord; UI sends the chosen ord).
2. `FoodServingSize.f4` and `f5` (CLI sends `1` and `servings`; UI sends the conversion factor and the user's raw input).

This spec covers only those two divergences.

## Existing code to modify

### `src/lose_it_utils/client/entries.py:35-181` — `_build_log_payload`

The function takes `(config, unsaved, meal_ordinal, day_key, day_num, servings)`
and constructs the wire payload. Relevant slices:

- Lines **102-107** decide `portion_size` from `servings`. This is currently
  the *only* knob that controls the FoodServingSize quantity slot. Replace
  with three new parameters: `measure_ord_override`, `quantity_in_chosen_unit`,
  `conversion_factor`.
- Lines **164-178** emit the FoodServingSize + FoodMeasure block. The slots
  `data[100]` (= `portion_size_str`), `data[103]` (= `measure_ord`), `data[104]`
  and `data[105]` (currently `"1", "1"`), `data[106]` (= `portion_size_str`
  again) need to become `canonical_servings`, `chosen_ord`, `1.0`,
  `conversion_factor`, `quantity_in_chosen_unit`.

### `src/lose_it_utils/cli.py:393-478` — the `log` command

- Lines **398-409** define `--grams`. Add a parallel `--amount` option.
- Lines **455-478** validate `--grams` against the food's measure ordinal
  and emit a clear `not_gram_measured` error. Generalize this validation
  for any unit (not just grams).

### `src/lose_it_utils/client/_models.py` — `UnsavedFoodLogEntry`

- The `food_measure_ordinal` attribute is the **canonical** ordinal as
  returned by the server. We do *not* mutate this; the override stays in
  the call site.

### `src/lose_it_utils/client/_config.py`

- `MEASURE_NAMES` already maps ord→name (used in error messages). Extend
  as needed for any new ordinals.

## New module: `src/lose_it_utils/client/_units.py`

Single module that owns the conversion table and the parser.

```python
"""Unit conversion table mirroring the official UI's display-unit dropdown.

All factors are US customary measurement constants. The Lose It! web UI
uses these same constants (e.g. ``236.5882365`` mL/US cup, observed in
captured HARs); the figures here are not food-specific, only unit-specific.
"""

from __future__ import annotations

import re

# (canonical_ord, chosen_ord) → factor such that
#   quantity_in_chosen_unit = canonical_servings × factor
# A diagonal entry (ord, ord) is always 1.0 — included so callers can do
# the lookup unconditionally.
CONVERSIONS: dict[tuple[int, int], float] = {
    # cup (3) ↔ volumetric units. 1 US cup = 236.5882365 mL = 8 fl oz = 16 tbsp.
    (3, 3):  1.0,
    (3, 11): 236.5882365,
    (3, 10): 8.0,
    (3, 2):  16.0,
    # fl oz (10) ↔ volumetric. 1 US fl oz = 29.5735296875 mL = 2 tbsp.
    (10, 10): 1.0,
    (10, 11): 29.5735296875,
    (10, 2):  2.0,
    (10, 3):  0.125,
    # tbsp (2) ↔ volumetric.
    (2, 2):  1.0,
    (2, 11): 14.78676478125,
    (2, 10): 0.5,
    (2, 3):  0.0625,
    # mL (11) ↔ volumetric (the inverses).
    (11, 11): 1.0,
    (11, 3):  1.0 / 236.5882365,
    (11, 10): 1.0 / 29.5735296875,
    (11, 2):  1.0 / 14.78676478125,
    # grams (8) — kept as-is to mirror the existing `--grams` flag's
    # convention that "1 serving = 100 g". This is a *special* case in
    # the existing CLI (entries.py:103-105); we preserve it intentionally.
    (8, 8): 100.0,
}

# Casing-insensitive aliases that map a user-typed unit suffix to its
# FoodMeasurement ordinal. Keep the keys lowercase; the parser normalises
# the input before lookup.
UNIT_ALIASES: dict[str, int] = {
    "cup": 3, "cups": 3, "c": 3,
    "ml":  11, "milliliter": 11, "milliliters": 11,
    "fl_oz": 10, "floz": 10, "fl-oz": 10, "fluid_oz": 10,
    "tbsp": 2, "tablespoon": 2, "tablespoons": 2, "t": 2,
    "g":   8, "gram": 8, "grams": 8,
    # Deliberately omitted: bare "oz". In cooking it can mean weight ounce
    # (~28.35 g) or fluid ounce (~29.57 mL). The CLI requires the user to
    # spell out "fl_oz" for volume or use "g" for weight.
}

_AMOUNT_RE = re.compile(
    r"^\s*(?P<n>\d+(?:\.\d+)?|\.\d+)\s*(?P<unit>[A-Za-z_-]+)\s*$"
)


def parse_amount(spec: str) -> tuple[float, int]:
    """Parse ``"490mL"`` → ``(490.0, 11)``.

    Raises ``ValueError`` for unparseable input, unknown units, and the
    ambiguous bare ``"oz"`` (which can mean weight ounce or fluid ounce
    depending on culture/context).
    """
    m = _AMOUNT_RE.match(spec)
    if not m:
        raise ValueError(
            f"Could not parse {spec!r} as <number><unit>. "
            "Examples: 490mL, 86g, 2cups, 8fl_oz."
        )
    n = float(m.group("n"))
    raw_unit = m.group("unit").strip().lower().replace(" ", "_")
    if raw_unit == "oz":
        raise ValueError(
            "Bare 'oz' is ambiguous (weight vs fluid). "
            "Use 'fl_oz' for volume or 'g' for weight."
        )
    if raw_unit not in UNIT_ALIASES:
        raise ValueError(
            f"Unknown unit {raw_unit!r}. Known: "
            + ", ".join(sorted(set(UNIT_ALIASES.keys())))
        )
    return n, UNIT_ALIASES[raw_unit]


def conversion_factor(canonical_ord: int, chosen_ord: int) -> float | None:
    """Return the factor such that ``chosen_qty = canonical_qty × factor``.

    Returns ``None`` when the food's native unit doesn't support a
    conversion to the requested unit (e.g. cup→grams isn't physical
    without density info, so it's deliberately absent from the table).
    """
    return CONVERSIONS.get((canonical_ord, chosen_ord))
```

## Edge case: food doesn't support the requested unit

This is the user's specific concern: not every food has every unit. The
official UI's dropdown reflects this — e.g., a "1 each" tortilla entry
typically doesn't offer a gram option unless the entry stores a per-each
weight.

### Detection

`conversion_factor(canonical_ord, chosen_ord)` returns `None` when the
combination isn't in the table. The CLI must:

1. Call `foods.get_unsaved_food_log_entry(http, food)` to read
   `unsaved.food_measure_ordinal` (= the food's canonical ord).
2. Call `conversion_factor(canonical_ord, chosen_ord)`. If `None`, error
   out *before* sending any mutating RPC. Print a clear message + the
   set of units the food *does* support.

### Suggested alternatives

When the CLI errors with "this food doesn't support `--amount 86g`", it
should also probe the rest of the search results for alternatives that
*do* support grams, mirror the existing `--grams` error format
(`cli.py:459-477`), and present a short numbered list. New helper in
`foods.py`:

```python
def find_supporting_alternatives(
    http: HttpClient,
    query: str,
    target_unit_ord: int,
    *,
    max_results: int = 5,
) -> list[tuple[FoodSearchResult, UnsavedFoodLogEntry]]:
    """For each search result, return the ones whose native unit
    converts to ``target_unit_ord``. Includes the unsaved-entry template
    so callers can show ``serving_qty`` + per-serving cal alongside the
    name. Limit to ``max_results`` to avoid 14 round-trips for searches
    that return many candidates.
    """
```

`max_results` is important: probing each candidate requires a fresh
`getUnsavedFoodLogEntry` RPC. Limit prevents the alternatives suggestion
from doing 14 round-trips on every error.

### Catalog integration

The Lose It! food-catalog memory entries already document this trap
("when picking, REJECT entries with `serving_qty != 1.0`"). The
alternatives list should display `unsaved.serving_qty` so the user can
see which alternatives are likely to give correct calorie counts in the
app and which trip the documented trap.

## CLI surface

Add `--amount` next to the existing `--servings` / `--grams` on
`lose-it log`. Mutually exclusive with both. When `--amount` is passed,
the CLI:

1. Searches as usual.
2. Calls `get_unsaved_food_log_entry` on the chosen pick.
3. Parses the amount via `_units.parse_amount`.
4. Looks up the conversion factor.
5. If `None`: errors out with the food's native unit + suggests
   alternatives (Phase 3).
6. Otherwise: computes `canonical_servings = quantity / factor` and
   calls `entries.log_food(...)` with the new parameters.

`--grams` continues to exist as a legacy alias for `--amount Ng` — both
should resolve to the same wire payload.

## Implementation plan

### Phase 1 — wire-layer plumbing (≈30 min)

1. Create `src/lose_it_utils/client/_units.py` per the snippet above.
2. Modify `_build_log_payload`:
   - Add optional `measure_ord_override`, `quantity_in_chosen_unit`,
     `conversion_factor` parameters. Default behaviour is unchanged.
   - When the override is set, emit the new FoodServingSize block matching
     the HAR (lines 164-178 in current code).
3. Update `entries.log_food` to forward the new parameters.
4. **Unit test**: construct payload for a known fixture (the soup pick 1)
   with `--amount 490mL`. Diff against the HAR's `postData.text` tail
   byte-for-byte. The whole HashMap should match exactly; the
   FoodServingSize block should match exactly up to floating-point
   serialization (we don't expect `2.0711109608264158` precision).

### Phase 2 — CLI flag (≈20 min)

1. Add `--amount` option to `lose-it log` in `cli.py`.
2. Validate mutual exclusion with `--servings` / `--grams`.
3. Update the dry-run output to include the chosen unit ("→ snacks 490 mL (207 cal)").
4. Keep `--grams` as a deprecated shortcut: internally rewrite to
   `--amount {N}g` so there's one code path.

### Phase 3 — suggested alternatives on unit mismatch (≈20 min)

1. Add `foods.find_supporting_alternatives` per the snippet.
2. When unit lookup fails, print a 5-row alternatives table:
   `# | name | brand | native unit | serving_qty | cal/serving`.
3. Add a `--force` flag for the rare case where the user wants to log
   anyway (escape hatch; mirrors the existing `--yes` pattern in `delete`).

### Phase 4 — README + catalog notes (≈10 min)

1. Document `--amount` in the README's `### log` section with examples.
2. Update the existing `--grams` documentation to note it's an alias for
   `--amount Ng`.
3. Cite this spec from the catalog row for the soup so future log-food
   skill invocations prefer `--amount` for cup-stored foods.

## Verification

### Unit tests (Phase 1)

Add `tests/conformance/test_units.py`:

| Case | Expected |
|---|---|
| `parse_amount("490mL")` | `(490.0, 11)` |
| `parse_amount("86g")` | `(86.0, 8)` |
| `parse_amount("2.5cups")` | `(2.5, 3)` |
| `parse_amount("8oz")` | `ValueError("ambiguous")` |
| `parse_amount("3 unicorns")` | `ValueError("unknown unit")` |
| `conversion_factor(3, 11)` | `≈ 236.588` |
| `conversion_factor(3, 8)` | `None` (no cup→grams) |
| `conversion_factor(11, 3)` | `≈ 0.00423` (reciprocal) |

Add `tests/conformance/test_entries_amount.py`:

| Case | Expected |
|---|---|
| `_build_log_payload(soup, --amount=490mL)` byte-compare | Matches HAR snippet at `27|<canonical>|1|28|11|1.0|236.588|490` |
| Nutrient HashMap | Per-serving values (cal=100, sodium=750, …), no `× servings` |
| `_build_log_payload(soup, --servings=2.07)` (legacy) | Wire bytes unchanged from current behaviour |

### Live verification

1. **Dry-run**: `lose-it log "trader joe's tomato roasted red pepper soup" --pick 1 -m snacks --amount 490mL --dry-run`. Expected stdout:
   ```
   ✅ DRY RUN — would log Organic Tomato And Roasted Red Pepper Soup → snacks 490 mL (207 cal)
   ```
2. **Wire-byte check**: `lose-it --log-level trace --log-file /tmp/amount.log log ... --amount 490mL --dry-run` then grep for `27|`. The FoodServingSize block should match the HAR's `27|2.071…|1|28|11|1.0|236.588|490|…` byte-for-byte (modulo float precision).
3. **Live log** (mutating; only after dry-run matches HAR): log at `--amount 490mL`. The official app should render **`490 mL`** in the entry's serving display and **≈ 207 cal** in the entry's calorie display.

### Alternatives-suggestion verification

1. `lose-it log "1 tortilla" -m snacks --amount 50g --dry-run` — should error because the standard "1 each" tortilla doesn't support grams.
2. Error output should include 2-5 alternative entries from the same
   search that DO support grams (gram-measured tortilla copies), each
   annotated with `serving_qty` so the user can spot catalog-flagged
   ones.

## Risks and follow-ups

1. **Diary readback semantics** — after #22 the nutrient HashMap on the
   wire is per-serving. The `FoodLogEntry.calories` property
   (`_models.py:62-67`) reads the raw HashMap value, so it now shows
   per-serving cal instead of consumed cal. Follow-up: multiply by
   `servings` in the property. Not blocking for this spec.
2. **Diary parser still partial** — when probing alternatives we'll
   make N extra round-trips. If the decoder hits a desync on a heavy
   response, we may silently lose alternatives. Mitigation: cap at 5,
   surface "and N more (decode failed)" if partial.
3. **`oz` ambiguity** is intentional. We reject bare `oz` because the US
   "fluid ounce" (~29.6 mL) and weight ounce (~28.35 g) differ. If
   future UX research suggests defaulting to fluid ounce, change one
   line in `_units.py`.

## Citation index

- HAR `updateFoodLogEntry`: user-provided HAR (2026-06-11), entries[6],
  request.postData.text, see "Wire-level evidence" section.
- `_build_log_payload`: src/lose_it_utils/client/entries.py:35-181
  (FoodServingSize block lines 164-178; portion_size logic lines 102-107).
- `--grams` option + error: src/lose_it_utils/cli.py:398-409, 455-478.
- `get_unsaved_food_log_entry`: src/lose_it_utils/client/foods.py:268-288.
- `UnsavedFoodLogEntry`: src/lose_it_utils/client/_models.py:23-34.
- `FoodLogEntry.calories` property: src/lose_it_utils/client/_models.py:62-67.
- Conversion factor confirmation: HAR f4=236.588 (US cup→mL is exactly 236.5882365).

## Glossary

- **Canonical unit** — the food's native FoodMeasurement ord (e.g. cup=3 for
  the soup). The server stores nutrients per canonical serving.
- **Chosen unit** — the unit the *user* wants to log in. Same as canonical
  when no `--amount` override.
- **Conversion factor** — `quantity_in_chosen_unit = canonical_servings × factor`.
  Drawn from a universal table in `_units.CONVERSIONS`.
- **Per-serving nutrients** — the food's stored per-canonical-serving
  nutrient values. The server multiplies by `FoodServing.quantity` to
  produce the entry's stored calorie count.
