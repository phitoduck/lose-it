# Changelog

## Unreleased

### Added

- `loseit add-recipe SPEC` / `LoseIt.create_recipe`: create a recipe in the Recipes list from a JSON spec (name, yield, ingredients by `query`/`food_id` sized by `servings` or `serving_amount` + `serving_unit`). Resolves every ingredient before writing, then sends one `saveRecipe` RPC mirroring the web client's payload; `--dry-run` previews totals and per-serving calories.

- `--grams-per-ml` on `loseit log` (and `grams_per_ml` on recipe ingredients / `LoseIt.log_food`): a density that converts between volume and weight for foods Lose It! stores only by volume (honey, cocoa powder, date syrup) — or only by weight. Previously `--serving-unit g` was impossible for them.
- `loseit describe-food --query Q [--pick N]`: describe search hits directly instead of copying IDs. Every described food now lists its `supported_units`.
- `unit_not_supported` errors list the units the food does support (and say when `--grams-per-ml` would help); `add-recipe` prefixes them with the ingredient number.

### Changed

- `loseit log QUERY --food-id ID` is now allowed (still exclusive with `--pick`): the ID picks the food, the query only supplies its name when the ID lookup has none. Recipe ingredients likewise accept `food_id` and `query` together.

### Fixed

- `--food-id` / recipe `food_id` / `describe-food` failed with "Food with id … not found" for many generic foods ("Almond Butter", "Honey"): `getFood` returns them without a name, which was mistaken for a missing food. They now resolve; writes that would need the name ask for the search query that found the food (`food_name_unavailable`) instead of creating a blank entry.
- `--serving-unit tsp` didn't work through a food's per-serving mL slot (the mL → unit fan-out skipped teaspoons).
- Every RPC failed with `IncompatibleRemoteServiceException` after Lose It!'s redeploy to permutation `201A8433…`. Default `policy_hash` / `strong_name` and `_schemas.json` refreshed (signatures retired by the redeploy are kept so older captures still decode).
- `tools/extract_gwt_schemas.py` couldn't find the read primitives in the new bundle: it matched helpers by their minified names, which change on every build. It now matches them by body shape.

- `loseit delete` / `LoseIt.delete_entry` returned HTTP 500 for entries whose diary response carries no `DoXxxx` food identifier code — the empty string serialized as an empty token in the `deleteFoodLogEntry` envelope. Blank codes now fall back to `"AAAAAA"` (base64 zero-long), same placeholder trick as the DayDate fallback key. ([#78](https://github.com/phitoduck/lose-it/pull/78))
- Diary parser dropped entries past the first Timestamp-bearing one — `java.sql.Timestamp` consumes 2 tokens, not 1. `loseit diary --date 2025-07-15` → 0 entries despite 14 KB of wire data. Year-mining: 17 → 1,211 entries. ([#33](https://github.com/phitoduck/lose-it/pull/33))
- `loseit delete` always returned HTTP 500 — `entry_day_key` was scraped as `'Honey'`/`'Tomato'` instead of the base64 epoch-long from `Date.raw`. ([#33](https://github.com/phitoduck/lose-it/pull/33))
- Historical-date diary fetches returned HTTP 500 — `loseit diary --date 2025-06-15` sent `day_key=""` when not in init RPC's recent-day window. Fallback placeholder unblocks all dates. ([#33](https://github.com/phitoduck/lose-it/pull/33))

### Added

- `--serving-unit` accepts `tsp`, `can`, `container` (ord 1/21/45). ([#33](https://github.com/phitoduck/lose-it/pull/33))
