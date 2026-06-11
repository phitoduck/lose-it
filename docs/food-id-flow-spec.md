# Spec: `--food-id` flow for stable food references

This is a context-loss-resistant spec. Companion to
[`serving-unit-spec.md`](serving-unit-spec.md); the two compose but can
ship independently.

## Background

The current `lose-it log "<query>" --pick N` flow has two structural problems:

1. **Pick indices drift.** The same `search` query can return results in a
   different order between sessions (Lose It! mutates its index regularly).
   The user's food-catalog memory already documents this and has logged
   the drift on multiple foods.
2. **Natural-language queries are ambiguous** to the CLI. `lose-it log "1 tortilla" --serving-amount 50 --serving-unit g` doesn't tell the CLI *which*
   tortilla — it relies on `--pick` to disambiguate, which inherits the
   drift problem.

The food's `SimplePrimaryKey` (16 bytes) is the **stable** identifier the
server actually uses for lookups. The CLI already extracts it as
`FoodSearchResult.pk_bytes` (`foods.py:14-21`) and even surfaces it as a
list-of-ints in JSON search output (`cli.py:367-374`), but there's no
human-friendly encoding and no command path that consumes it.

This spec adds:

- A hex-encoded `food_id` field in `lose-it search` output (both text and
  JSON).
- A `--food-id <hex>` option on `lose-it log` that bypasses search and
  goes straight to the unsaved-entry RPC for the named PK.
- A thin `getFood` RPC client wrapper, used to materialise the food's
  name when the user only has a PK (since `getUnsavedFoodLogEntry`
  requires the name as a string-table entry).

## What already exists

- `FoodSearchResult.pk_bytes: list[int]` — `_models.py:13-21`. 16 signed
  ints in "response form" (the convention `_build_unsaved_payload`
  expects on input).
- `foods.get_unsaved_food_log_entry(http, food)` — `foods.py:268-288`.
  Takes a `FoodSearchResult` and returns the template. Internally calls
  `_build_unsaved_payload` (lines 156-188) which serialises the PK with
  `reversed(food.pk_bytes)`.
- `_build_unsaved_payload` also embeds `food.name` as a string-table
  entry (line 180). Without `food.name`, the request shape is invalid.
- `lose-it search` JSON output emits `pk_bytes` as a list of 16 ints
  (`cli.py:367-374`). Already public-API, just unergonomic to copy/paste.
- `getFood` is the RPC for "look up a food by PK". Observed in the
  official UI's wire (see "Wire-level evidence" below); we have no
  client wrapper for it yet.

## Wire-level evidence

Observed in 2026-06-11 manual testing: the official UI's `getFood`
request shape (when the user clicks a food in their personal-DB search
results) is:

```
7|0|11|https://d3hsih69yn4d89.cloudfront.net/web/|8F87EC8969F17AE77B6283D3A83F6D4C|com.loseit.core.client.service.LoseItRemoteService|getFood|com.loseit.core.client.service.ServiceRequestToken/1076571655|com.loseit.core.client.model.interfaces.IPrimaryKey|java.lang.String/2004016611|com.loseit.core.client.model.UserId/4281239478|eric.riddoch|com.loseit.core.client.model.SimplePrimaryKey/3621315060|[B/3308590456|1|2|3|4|3|5|6|7|5|0|8|53539329|9|-6|10|11|16|16|17|13|95|48|-65|66|49|-93|-38|-116|60|-106|8|-16|99|0|
```

String table (11 entries):

```
 1  https://d3hsih69yn4d89.cloudfront.net/web/
 2  8F87EC8969F17AE77B6283D3A83F6D4C            ← policy_hash
 3  com.loseit.core.client.service.LoseItRemoteService
 4  getFood                                       ← method
 5  com.loseit.core.client.service.ServiceRequestToken/1076571655
 6  com.loseit.core.client.model.interfaces.IPrimaryKey
 7  java.lang.String/2004016611
 8  com.loseit.core.client.model.UserId/4281239478
 9  eric.riddoch                                  ← user_name
10  com.loseit.core.client.model.SimplePrimaryKey/3621315060
11  [B/3308590456                                 ← byte array marker
```

Data section (decoded):

```
1|2|3|4|3|5|6|7|         ← envelope (3 args follow)
5|0|8|53539329|9|-6|     ← UserId(53539329, "eric.riddoch", hrs_from_gmt=-6)
10|11|16|                ← SimplePrimaryKey wrapping a byte array of len 16
16|17|13|95|48|-65|66|49|-93|-38|-116|60|-106|8|-16|99|   ← 16 raw bytes
0|                        ← terminator
```

Two observations:

1. The wire shape is **just a UserId + a 16-byte PK** — no name, no extra
   metadata. Mirrors `_build_unsaved_payload` minus the name/locale entries.
2. The response contains the food's full `FoodIdentifier` (name, brand,
   category, FoodProductType, Verification, native PK). The same shape
   `daily.py`/`foods.py` already decode for entries. We can reuse the
   schema decoder unchanged.

## Encoding the food ID

PK is **16 bytes** (`pk_bytes: list[int]`, currently expressed as signed
ints in `[-128, 127]`). Encoding options:

| Encoding | Length | Notes |
|---|---|---|
| Hex (lowercase) | 32 chars | Standard, copy/paste-safe, no special chars. |
| Base32 | 26 chars | Slightly shorter. |
| Base64 (URL-safe) | 22 chars | Shortest. Includes `-`, `_`. |
| Raw decimal list | ~80 chars | Current JSON shape. Verbose. |

**Recommendation: lowercase hex.** Trade-off:

- 32 chars fits on one line and is easy to grep.
- No special characters that need shell-escaping.
- The user can copy directly from JSON tools (`jq`, etc.) without surprises.
- Round-trip tested below in "Verification".

Two-way conversion helpers:

```python
def pk_to_hex(pk_bytes: list[int]) -> str:
    """16 signed ints in [-128, 127] → 32-char lowercase hex."""
    return bytes((b & 0xFF) for b in pk_bytes).hex()


def hex_to_pk(hex_str: str) -> list[int]:
    """32-char lowercase hex → 16 signed ints (response-form pk_bytes)."""
    raw = bytes.fromhex(hex_str.strip().lower())
    if len(raw) != 16:
        raise ValueError(
            f"Food ID must be 32 hex chars (16 bytes); got {len(raw)} bytes."
        )
    return [b - 256 if b >= 128 else b for b in raw]
```

Place in `src/lose_it_utils/client/_ids.py` (new module).

## New module: `src/lose_it_utils/client/_ids.py`

```python
"""Food ID encoding/decoding (16-byte SimplePrimaryKey ↔ 32-char hex).

The SDK already represents PKs as ``list[int]`` in "response form"
(see ``FoodSearchResult.pk_bytes`` in ``_models.py``). This module is
the user-facing translation layer: humans see lowercase hex, the SDK
sees signed ints.
"""

from __future__ import annotations


def pk_to_hex(pk_bytes: list[int]) -> str:
    """Encode response-form PK bytes as 32-char lowercase hex."""
    if len(pk_bytes) != 16:
        raise ValueError(f"PK must be 16 bytes; got {len(pk_bytes)}.")
    return bytes((b & 0xFF) for b in pk_bytes).hex()


def hex_to_pk(food_id: str) -> list[int]:
    """Decode 32-char lowercase hex into response-form PK bytes.

    Accepts arbitrary case and trims whitespace. Raises ``ValueError``
    for non-hex input or length mismatches.
    """
    s = food_id.strip().lower()
    try:
        raw = bytes.fromhex(s)
    except ValueError as exc:
        raise ValueError(f"Food ID is not valid hex: {food_id!r}") from exc
    if len(raw) != 16:
        raise ValueError(
            f"Food ID must be 32 hex chars (16 bytes); got {len(raw)} bytes."
        )
    return [b - 256 if b >= 128 else b for b in raw]
```

## New RPC client: `foods.get_food`

```python
def get_food(http: HttpClient, pk_bytes: list[int]) -> FoodSearchResult:
    """Look up a food by its PK. Returns a ``FoodSearchResult``-compatible
    record (name, brand, category, pk_bytes) so callers can hand the
    result straight to ``get_unsaved_food_log_entry``.

    Wraps the ``getFood`` GWT-RPC method. Sends the standard UserId
    envelope + a SimplePrimaryKey wrapping the 16-byte PK. Decodes the
    response via the schema-driven decoder and pulls out the
    ``FoodIdentifier`` subtree.
    """
    payload = _build_get_food_payload(http.config, pk_bytes)
    text = http.post_rpc(payload)
    decoded = decode_response(text)
    identifier = next(
        (d for d in _walk(decoded, fqcn=_FOOD_IDENTIFIER) if d.get("__type__") == _FOOD_IDENTIFIER),
        None,
    )
    if identifier is None:
        raise LoseItError(f"Food with id {pk_to_hex(pk_bytes)} not found")
    return FoodSearchResult(
        name=identifier.get("f3") or "",
        brand=identifier.get("f4") or "",
        category=identifier.get("f1") or "",
        pk_bytes=pk_bytes,
    )


def _build_get_food_payload(config: Config, pk_bytes: list[int]) -> str:
    if len(pk_bytes) != 16:
        raise ValueError("food.pk_bytes must be 16 bytes")
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getFood",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.interfaces.IPrimaryKey",
        "java.lang.String/2004016611",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",
        "[B/3308590456",
    ]
    data: list[str] = ["1", "2", "3", "4", "3", "5", "6", "7"]
    data += ["5", "0", "8", config.user_id, "9", str(config.hours_from_gmt)]
    data += ["10", "11", "16"]
    data += [str(int(b)) for b in reversed(pk_bytes)]
    data += ["0"]
    return build_envelope(strings, data)
```

The byte layout matches the "Wire-level evidence" section's `data`
section exactly, except `config.user_id` and `pk_bytes` are interpolated.

## CLI surface changes

### `lose-it search`

Add `food_id` to both output formats.

**Text:**
```
  #  Food                                              Brand                  Food ID
───  ──────────────────────────────────────────────── ─────────────────────  ────────
  1  Power Cakes Flapjack And Waffle Mix, Buttermilk  Kodiak Cakes           9eba9129b8…
```

Truncate to ~10 chars in text mode (`{hex[:10]}…`) since 32 chars
wouldn't fit. Full hex available via `--output json`.

**JSON** (add `food_id` alongside the existing `pk_bytes` for one release
cycle, then drop `pk_bytes` in a follow-up):

```json
{
  "results": [
    {
      "name": "Power Cakes Flapjack And Waffle Mix, Buttermilk",
      "brand": "Kodiak Cakes",
      "category": "Pancakes",
      "food_id": "9eba9129b8494967c8cb3385acf0f614",
      "pk_bytes": [-98, -70, -111, 41, -72, 73, 73, 103, -56, -53, 51, -123, -84, -16, -10, 20]
    }
  ]
}
```

### `lose-it log`

Add `--food-id <hex>` option. Mutually exclusive with the positional
`query` + `--pick`. When set:

1. Decode hex via `_ids.hex_to_pk` (errors → exit code 2).
2. Call `foods.get_food(http, pk_bytes)` to materialise a
   `FoodSearchResult` with name/brand/category.
3. Proceed through `get_unsaved_food_log_entry` → `log_food` exactly as
   the search-based path does today.

Make the positional `query` *optional* via typer's `Annotated[str | None,
typer.Argument(...)]` so the user can run `lose-it log --food-id <hex>
--meal snacks --servings 1.0` without typing a placeholder string.

The pick-based path stays intact for ad-hoc use.

## Edge cases

| Case | Behaviour |
|---|---|
| `--food-id` is not 32 hex chars | exit 2, `Food ID must be 32 hex chars (16 bytes)` |
| `--food-id` contains non-hex chars | exit 2, `Food ID is not valid hex` |
| `--food-id` valid but PK doesn't exist on server | `getFood` returns empty FoodIdentifier; raise `LoseItError("Food with id … not found")` and exit 1 |
| Both `--food-id` and a `query` positional are passed | exit 2, `--food-id and <query> are mutually exclusive` |
| Both `--food-id` and `--pick` are passed | exit 2, same message |
| Neither `--food-id` nor a `query` | exit 2, `must pass either --food-id or a search query` |

## Composition with `serving-unit-spec.md`

The two specs compose cleanly: `lose-it log --food-id <hex>
--serving-amount 490 --serving-unit mL --meal snacks` is the
fully-disambiguated, drift-proof form. The food-catalog memory should
migrate to storing `food_id + (serving_amount, serving_unit)` tuples
instead of `(query, pick, ord, unit)` tuples, so re-runs don't need to
re-validate picks every session.

## Implementation plan

### Phase 1 — encoding helpers (≈10 min)

1. Create `src/lose_it_utils/client/_ids.py` per the snippet above.
2. Unit tests for round-trip: random 16-byte sequences encode/decode
   back to themselves; specific known PKs (e.g. the soup's
   `[13, 95, 48, -65, 66, 49, -93, -38, -116, 60, -106, 8, -16, 99, 0, …]`)
   encode to the expected hex.

### Phase 2 — `getFood` RPC client (≈20 min)

1. Add `_build_get_food_payload` + `get_food` to `foods.py`.
2. Unit test: construct payload for a known fixture PK and byte-compare
   against the "Wire-level evidence" snippet from this spec.
3. Live test: call `get_food` with a PK harvested from `lose-it search
   --output json`; verify the name comes back.

### Phase 3 — `lose-it search` output (≈15 min)

1. In `cli.search`:
   - Compute `food_id = pk_to_hex(r.pk_bytes)` for each result.
   - JSON output: add `"food_id": food_id` next to `pk_bytes`.
   - Text output (`_print_search_results` in `cli.py`): add a `Food ID`
     column with `{food_id[:10]}…`.

### Phase 4 — `lose-it log --food-id` (≈25 min)

1. Add `--food-id` option to `log`. Make `query` optional.
2. Validation: enforce mutual exclusion with `query`/`--pick`, decode
   hex, materialise via `get_food`, proceed as today.
3. Update dry-run + success-line output to include the food ID:
   ```
   ✅ Logged Organic Tomato And Roasted Red Pepper Soup (id 9eba…)
      → snacks 490 mL (207 cal)
   ```

### Phase 5 — docs + catalog migration (≈10 min)

1. Document `--food-id` in the README's `### log` section.
2. Add an example chained with `--serving-amount` / `--serving-unit`.
3. Note in the catalog-row format that `food_id` is preferred to
   `query + pick` for stable references.

## Verification

### Unit tests

`tests/conformance/test_ids.py`:

| Case | Expected |
|---|---|
| `pk_to_hex([-98, -70, -111, 41, …])` | `'9eba9129…'` (full 32 chars) |
| `hex_to_pk("9eba…")` | `[-98, -70, -111, 41, …]` (matches input) |
| `hex_to_pk("9EBA9129…")` (uppercase) | same as lowercase |
| `hex_to_pk("not-hex")` | `ValueError` |
| `hex_to_pk("9eba")` (too short) | `ValueError` |
| Round-trip a thousand random PKs | always equal |

`tests/conformance/test_get_food.py`:

| Case | Expected |
|---|---|
| `_build_get_food_payload(cfg, pk)` byte-compare | matches the wire-evidence snippet in this spec |
| `get_food(http, pk)` on a mocked fixture | returns FoodSearchResult with name/brand from FoodIdentifier |

### CLI tests

| Invocation | Expected |
|---|---|
| `lose-it search "kodiak"` (text) | output table includes a `Food ID` column |
| `lose-it search "kodiak" -o json | jq '.results[0].food_id'` | non-empty 32-char hex |
| `lose-it log --food-id 9eba…0123 -m snacks --servings 1` | succeeds, success line includes `(id 9eba…)` |
| `lose-it log --food-id NOTHEX -m snacks --servings 1` | exit 2, "not valid hex" |
| `lose-it log --food-id 9eba --pick 1 "kodiak"` | exit 2, "mutually exclusive" |
| `lose-it log -m snacks --servings 1` (no query, no id) | exit 2, "must pass either" |

### Live verification

1. `lose-it search "trader joe's tomato roasted red pepper soup" -o json | jq -r '.results[0].food_id'` → captures a hex ID, e.g. `0d5f30bf4231a3da8c3c9608f06300xy`.
2. `lose-it log --food-id <captured> -m snacks --servings 1.0 --dry-run` → dry-run preview includes the correct food name and ID.
3. Live log; verify entry appears in the official app under the right
   name. (The PK is the authoritative identifier; if `getFood`
   round-trips correctly, this WILL work.)

### Composition test

`lose-it log --food-id <hex> -m snacks --serving-amount 490 --serving-unit mL` → identical wire payload to the spec in
[`serving-unit-spec.md`](serving-unit-spec.md), modulo the absence of a
search RPC round-trip.

## Risks and follow-ups

1. **PK drift across data migrations.** Lose It! occasionally
   regenerates internal IDs (a personal-DB copy gets a new PK after a
   server-side migration). Mitigation: catalog rows can store both
   `food_id` and the pinning `query + brand` fallback. Out of scope
   for this spec.
2. **`getFood` not yet schema-tested.** No fixture for this RPC's
   response exists in `tests/conformance/fixtures/`. Phase 2's unit test
   should capture one via the existing functional-test path
   (`LOSEIT_RUN_FUNCTIONAL=1`).
3. **Personal-DB vs public-DB PKs.** The PK uniquely identifies a food
   *across both databases* (verified empirically against the user's
   captured PK `0d5f30bf4231a3da8c3c9608f06300…`, which is a personal-DB
   copy and round-trips through `getFood` the same way a public-DB PK
   would). No code change needed; just worth documenting.
4. **`lose-it info <food-id>`** as a future read-only inspection
   command (just calls `getFood` and pretty-prints). Trivial follow-up
   once `get_food` exists.

## Citation index

- `lose-it search` JSON `pk_bytes` exposure: src/lose_it_utils/cli.py:367-374.
- `lose-it log` `query` positional: src/lose_it_utils/cli.py:382-388.
- `FoodSearchResult.pk_bytes`: src/lose_it_utils/client/_models.py:13-21.
- `_build_unsaved_payload` (template for `_build_get_food_payload`):
  src/lose_it_utils/client/foods.py:156-188.
- `get_unsaved_food_log_entry` (consumer of the
  ``FoodSearchResult`` produced by `get_food`): src/lose_it_utils/client/foods.py:268-288.
- `getFood` wire shape (observed 2026-06-11): see "Wire-level evidence"
  in this file.
- Companion spec for the unit-conversion flow:
  [`docs/serving-unit-spec.md`](serving-unit-spec.md).

## Glossary

- **Food ID** — the human-facing alias for the food's 16-byte
  `SimplePrimaryKey`, encoded as 32-char lowercase hex.
- **PK / pk_bytes** — the SDK-internal representation as 16 signed
  ints (`list[int]`).
- **`getFood`** — the GWT-RPC method that returns a `FoodIdentifier`
  given a PK. Not currently wrapped by `lose_it_utils.client`.
