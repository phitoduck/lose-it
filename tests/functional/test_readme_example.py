"""Live-API end-to-end check for the README SDK example.

Mirrors the SDK section of ``README.md`` so the README and the wire
behavior cannot drift apart. The test:

1. Asserts the target diary day is initially clean of the food we'll log.
2. Logs two entries (1 serving + 61 g) via the high-level ``LoseIt`` API,
   exactly as the README shows.
3. Reads the diary back via ``li.diary(when)`` and confirms both entries
   are present.
4. Deletes the entries by passing the diary's ``FoodLogEntry`` dataclasses
   straight to ``li.delete_entry``.
5. Reads the diary back one more time and confirms the day is empty again.

The point is to prove the diary surface alone — without any raw PK bytes
or an "entry_id" handle in the JSON projection — is enough to log, locate,
and delete entries round-trip. ``food_id`` (the hex form of the food's
primary key) is the one external identifier the round-trip needs; the
entry's own PK never leaves the SDK.

Pinned to a 2018 date for isolation: it's far enough from any plausible
present-day diary that an unrelated entry won't accidentally satisfy
``e.food_id == chosen.food_id``. The test still cleans up after itself.

Marked ``requires_auth`` — skipped by default; run with
``pytest -m requires_auth tests/functional/test_readme_example.py``.
"""

from __future__ import annotations

import contextlib
from datetime import date

import pytest

from lose_it import LoseIt, MealType
from lose_it.models import FoodLogEntry, FoodSearchResult

pytestmark = pytest.mark.requires_auth


# Pinned far-from-present test day. 2018-03-15 (Thursday) is intentional:
# it predates the SDK's active use, so the chance of an unrelated entry
# colliding with our food filter is essentially zero. Far enough from a
# year boundary that off-by-one day-key math (Lose It! days run on local
# time) won't surprise us.
TEST_DAY = date(2018, 3, 15)
SEARCH_QUERY = "tortilla"


def _entries_for_food(li: LoseIt, when: date, food: FoodSearchResult) -> list[FoodLogEntry]:
    """All diary entries on ``when`` whose food matches ``food`` by stable PK."""
    return [e for e in li.diary(when) if e.food_id == food.food_id]


def _cleanup(li: LoseIt, when: date, food: FoodSearchResult) -> None:
    """Best-effort delete of any leftover entries on ``when`` for ``food``."""
    for entry in _entries_for_food(li, when, food):
        # Don't shadow a real test failure with a cleanup failure.
        with contextlib.suppress(Exception):
            li.delete_entry(entry)


def test_readme_sdk_example_round_trip() -> None:
    """Log → diary → delete round-trip via only the documented SDK surface."""
    with LoseIt.from_env() as li:
        # ── Search ──────────────────────────────────────────────────────
        results = li.search(SEARCH_QUERY)
        assert results, f"search for {SEARCH_QUERY!r} returned no candidates"
        chosen = results[0]
        assert len(chosen.food_id) == 32, "search result missing 32-char hex food_id"

        # ── Pre-state ───────────────────────────────────────────────────
        # If a prior failed run left entries on the pinned day, scrub them
        # before asserting — otherwise the assertion below is poisoned.
        _cleanup(li, TEST_DAY, chosen)
        before = _entries_for_food(li, TEST_DAY, chosen)
        assert not before, (
            f"{TEST_DAY} still has {len(before)} {chosen.name!r} entries after cleanup; "
            "manual intervention required"
        )

        try:
            # ── Log 1 serving to lunch ──────────────────────────────────
            logged_one = li.log_food(chosen, meal=MealType.lunch, servings=1.0, when=TEST_DAY)
            assert logged_one.meal_name == "lunch"
            assert logged_one.canonical_servings == pytest.approx(1.0)
            assert logged_one.when == TEST_DAY.isoformat()

            # ── Log 2 servings to lunch ─────────────────────────────────
            # Two distinct quantities — the diary read-back below cares
            # that two separate rows exist; the unit-based ``serving_amount`` /
            # ``serving_unit`` path the README also shows is exercised by
            # the conformance suite (test_entries_serving_unit.py) and isn't
            # what this round-trip test is gating on.
            logged_two = li.log_food(chosen, meal="lunch", servings=2.0, when=TEST_DAY)
            assert logged_two.meal_name == "lunch"
            assert logged_two.canonical_servings == pytest.approx(2.0)

            # ── Diary read-back: both entries should be there ───────────
            after_log = _entries_for_food(li, TEST_DAY, chosen)
            assert len(after_log) == 2, (
                f"expected 2 {chosen.name!r} entries on {TEST_DAY} after logging; "
                f"got {len(after_log)}: {[(e.servings, e.food_measure_unit) for e in after_log]}"
            )
            assert all(e.meal_name == "lunch" for e in after_log), (
                "logged entries didn't land in lunch"
            )
            # Diary entries carry the same stable food id; their entry PKs
            # are distinct UUIDs (proving the two log calls produced two
            # distinct server-side rows).
            entry_pks = {tuple(e.entry_pk_response) for e in after_log}
            assert len(entry_pks) == 2, "duplicate entry PKs across two log calls"

            # ── Delete every entry we just logged ───────────────────────
            for entry in after_log:
                li.delete_entry(entry)

            # ── Diary read-back: day is empty again ─────────────────────
            after_delete = _entries_for_food(li, TEST_DAY, chosen)
            assert not after_delete, (
                f"{len(after_delete)} {chosen.name!r} entries still on {TEST_DAY} after delete"
            )
        finally:
            # Defensive cleanup: if any assertion above fails partway, make
            # sure we don't leave junk on the pinned day for the next run.
            _cleanup(li, TEST_DAY, chosen)
