# Runbook: regenerate the GWT-RPC parser schemas

The decoder in `src/lose_it_utils/client/_decoder.py` is driven by
`_schemas.json`, which is extracted once from Lose It!'s compiled JS
bundle. The schemas only need refreshing when Lose It! redeploys their web
client.

## 1. When to re-run

Either symptom is a trigger:

- An RPC call raises `LoseItError("...IncompatibleRemoteServiceException...")`
  — the on-disk `policy_hash` / `strong_name` no longer match the server.
- The decoder raises `KeyError: "No schema for 'com.loseit...'"` — Lose It!
  added a new domain type since the last extraction.

You do **not** need to re-run on a cadence. Drive it off the two errors.

## 2. How to re-run + open PR

```bash
# 1. Find the current strong_name (= bundle hash for the `en` locale)
curl -s "https://d3hsih69yn4d89.cloudfront.net/web/web.nocache.js?v=$(date +%s)" \
  | grep -oE "ic='[0-9A-F]{32}'"
#  → ic='351AE5DC0CA36AD3BA9C7CBA7B0E07B8'

# 2. Confirm which deferred fragments the live app loads
#    (Open https://www.loseit.com in Chrome → DevTools → Network tab,
#     filter for `deferredjs/`. Note the integer fragment numbers — at the
#     time of writing the app loads 1, 2, 8, 10.)

# 3. Regenerate the schemas
python tools/extract_gwt_schemas.py \
  --permutation <strong_name_from_step_1> \
  --fragments 1 8 10 \
  --out src/lose_it_utils/client/_schemas.json

# 4. Update the hard-coded permutation defaults in
#    src/lose_it_utils/client/_settings.py (`strong_name` and
#    `policy_hash` — the latter is the 5th `|`-field of any
#    `/web/service` POST body; grab from DevTools).

# 5. Verify and PR
uv run pytest --no-cov
prek run --all-files
git checkout -b chore/refresh-parser-schemas
git add src/lose_it_utils/client/_schemas.json \
        src/lose_it_utils/client/_settings.py
git commit -m "chore(parser): refresh schemas for permutation <strong_name>"
git push -u origin HEAD
gh pr create --title "chore(parser): refresh GWT schemas after Lose It! redeploy"
```

## 3. Why this matters — concrete sample

Before the schema-driven decoder, `lose-it search "kodiak"` returned this
for row 6, picked by string-length / capitalization heuristics:

```
name     = "Power Cakes"
brand    = "Pancakes"
category = "Kodiak"
```

All three fields are wrong. The heuristic chose `"Power Cakes"` because
it was the longest string in scope, then `"Pancakes"` because it was the
next short string starting with a capital — exactly the opposite of what
the real `SearchResultFood` Java fields hold.

After the schema-driven decoder reads `SearchResultFood.f1`/`.f3`/`.f4` in
declaration order:

```
name     = "Power Cakes"
brand    = "Kodiak"
category = "Pancakes"
```

The downstream impact of the broken version: `lose-it log "kodiak" --pick 6`
serialized the wrong `FoodIdentifier` to the server, which either logged
the diary entry under the wrong food (silent data corruption) or rejected
the call with a generic `IncompatibleRemoteServiceException`. The schema
fix eliminates the class entirely — every field lands in its declared
slot by construction.

## 4. How often will this run?

**Estimate: weeks to months between redeploys.** Evidence:

- The current bundle was deployed **2026-06-04** (`Last-Modified` header
  on `<strong_name>.cache.js`). That's 7 days old as of 2026-06-11.
- Lose It! compiles with **GWT 2.8.2**, released April 2017. They haven't
  upgraded the compiler in 8+ years — strong signal that the web client
  is *not* on a continuous-deploy schedule.
- This repo has carried the same `strong_name` since the first commit
  (a872dd0, 2026-06-08) through to today with no redeploy observed.
- The `Cache-Control: max-age=31556926000` on the bundle (≈1000 years)
  reinforces the same conclusion: each bundle is content-addressed and
  effectively immutable, with new deploys served under a new URL.

Wayback Machine has no archived snapshots of the CloudFront origin
(`d3hsih69yn4d89.cloudfront.net`), so we cannot reconstruct a precise
historical cadence. The 2011–2012 snapshots of `www.loseit.com/web/`
predate the CloudFront migration and aren't relevant.

In short: don't put this on a cron. Wait for the failure mode in §1 and
react.
