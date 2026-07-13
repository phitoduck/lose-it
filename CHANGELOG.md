# Changelog

## Unreleased

### Fixed

- `get_food` / `describe-food` / log-by-food-id failed with "Food not found" after the server stopped returning name/brand in `getFood` responses (rolled out 2026-07-13). A null name now falls back to the category string; nutrition and serving sizes still resolve by PK. ([#79](https://github.com/phitoduck/lose-it/pull/79))
- `loseit delete` / `LoseIt.delete_entry` returned HTTP 500 for entries whose diary response carries no `DoXxxx` food identifier code — the empty string serialized as an empty token in the `deleteFoodLogEntry` envelope. Blank codes now fall back to `"AAAAAA"` (base64 zero-long), same placeholder trick as the DayDate fallback key. ([#78](https://github.com/phitoduck/lose-it/pull/78))
- Diary parser dropped entries past the first Timestamp-bearing one — `java.sql.Timestamp` consumes 2 tokens, not 1. `loseit diary --date 2025-07-15` → 0 entries despite 14 KB of wire data. Year-mining: 17 → 1,211 entries. ([#33](https://github.com/phitoduck/lose-it/pull/33))
- `loseit delete` always returned HTTP 500 — `entry_day_key` was scraped as `'Honey'`/`'Tomato'` instead of the base64 epoch-long from `Date.raw`. ([#33](https://github.com/phitoduck/lose-it/pull/33))
- Historical-date diary fetches returned HTTP 500 — `loseit diary --date 2025-06-15` sent `day_key=""` when not in init RPC's recent-day window. Fallback placeholder unblocks all dates. ([#33](https://github.com/phitoduck/lose-it/pull/33))

### Added

- `--serving-unit` accepts `tsp`, `can`, `container` (ord 1/21/45). ([#33](https://github.com/phitoduck/lose-it/pull/33))
