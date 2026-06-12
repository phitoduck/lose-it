# Changelog

Brief per-fix entries with a minimal repro and the fix.

## Unreleased

### Fixed

- **Diary parser dropped most entries past the first Timestamp-bearing entry.**
  `java.sql.Timestamp` consumes 2 tokens (millis from `instantiate`, nanos from
  `deserialize`); schema only modeled the deserialize, so the cursor desynced.
  Real-world impact: mining a year of diary returned ~17 entries when the user
  had logged ~1,200. ([decoder.py][df1])
  ```
  loseit diary --date 2025-07-15  → 0 entries (server returned 14 KB of data)
  ```
  Fix: inline handler pops both tokens (mirroring how `java.util.Date` is
  inlined). Conformance: `tests/conformance/test_timestamp_and_day_key.py`.

- **`loseit delete` returned HTTP 500 on every entry.** Parser extracted
  `entry_day_key`/`context_day_key` by scanning the FLE subtree for any
  4–16 char alphanumeric string — routinely picked up category labels
  (`'Honey'`, `'Tomato'`, `'Avocado'`) instead of the base64-encoded epoch-long
  the server uses as a cache key. ([daily.py][df2])
  ```
  loseit delete --meal breakfast --pick 1 --yes  → HTTP 500 (entry_day_key='Honey')
  ```
  Fix: `_DATE` inline handler now preserves the raw token alongside the
  decoded millis; parser pulls `DayDate.f0.raw` directly.

- **Historical-date diary fetches returned HTTP 500.** Server requires a
  non-empty `day_key` in the daily-details payload but doesn't validate
  its content — only `day_num` resolves the day. `getInitializationData`
  only returns day_keys for ~30 recent days plus sparse weekly history,
  so any older date sent `day_key=""` and crashed.
  ```
  loseit diary --date 2025-06-15  → HTTP 500: The call failed on the server
  ```
  Fix: `get_daydate_key` returns placeholder `"ZZZZZZZ"` when no exact
  match found. Conformance: `tests/conformance/test_init_day_key_fallback.py`.

### Added

- **FoodMeasurement labels for ord=1 (TEASPOON), ord=21 (CAN), ord=45 (CONTAINER).**
  Confirmed via broad food-DB probe (60 queries × 8 results). Examples:
  Raw Honey "1 Teaspoon" `per_serving_ml=4.92892` (= 1 US tsp), Pepsi
  `per_serving_ml=355` (= 12 fl oz US can), Chobani "Indiv. Container".
- **`--serving-unit` accepts `tsp`/`can`/`container`** in `loseit log`,
  with cup ↔ tbsp ↔ tsp ↔ fl_oz ↔ mL same-class conversion factors.
- **CHANGELOG.md** — this file.

[df1]: src/lose_it/client/_decoder.py
[df2]: src/lose_it/client/daily.py
