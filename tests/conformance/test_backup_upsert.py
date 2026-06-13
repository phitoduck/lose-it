"""Conformance tests for the safe-mode restore upsert match (T7).

Pure-function tests — no network, no filesystem. Asserts the math
behind spec §4.4 / §7.1 (with the modified_at substitution called out
in :mod:`lose_it.backup._upsert`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from lose_it.backup._fs import GrainEntry
from lose_it.backup._upsert import (
    DEFAULT_UPSERT_WINDOW,
    UpsertMatch,
    food_id_from_food_log_entry,
    plan_day,
    upsert_match,
)
from lose_it.core._ids import hex_to_pk
from lose_it.models import FoodLogEntry


def make_grain_entry(*, food_id: str, modified_at: str, **overrides) -> GrainEntry:
    """Build a minimum GrainEntry for tests."""
    base = dict(
        date=date(2016, 2, 15),
        day_num=6435,
        meal="snacks",
        meal_ordinal=3,
        food_id=food_id,
        food_name="Test",
        food_brand="Test",
        food_category="Test",
        food_identifier_code="DoTest",
        food_measure_ordinal=27,
        food_measure_unit="serving",
        servings=1.0,
        calories=70.0,
        nutrients={},
        nutrients_by_label={},
        entry_pk_response=[],
        food_pk_response=[],
        entry_day_key="",
        context_day_key="",
        hours_from_gmt=-6,
        created_at="",
        modified_at=modified_at,
        ingest_ts="",
    )
    base.update(overrides)
    return GrainEntry(**base)


def make_food_log_entry(
    *, food_pk_hex: str, modified_at: datetime | None, **overrides
) -> FoodLogEntry:
    """Build a minimum FoodLogEntry for tests.

    ``food_pk_hex`` is the 32-char hex that should round-trip to the
    grain entry's ``food_id``.
    """
    base = dict(
        food_category="Test",
        food_name="Test",
        food_brand="Test",
        food_pk_response=list(reversed(hex_to_pk(food_pk_hex))),
        entry_pk_response=[0] * 16,
        entry_day_key="",
        context_day_key="",
        day_num=6435,
        hours_from_gmt=-6,
        meal_ordinal=3,
        extra_ordinal=3,
        food_measure_ordinal=27,
        servings=1.0,
        food_identifier_code="DoTest",
        nutrients_ordered=[],
        modified_at=modified_at,
        created_at=None,
    )
    base.update(overrides)
    return FoodLogEntry(**base)


# ── upsert_match ─────────────────────────────────────────────────────────────


def test_match_within_window_returns_true():
    """Same food_id, 8 minutes apart -> match."""
    g = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    s = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 8, 0, tzinfo=UTC),
    )
    assert upsert_match(g, s) is True


def test_match_outside_window_returns_false():
    """Same food_id, 11 minutes apart -> no match (default 10-minute window)."""
    g = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    s = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 11, 0, tzinfo=UTC),
    )
    assert upsert_match(g, s) is False


def test_different_food_id_never_matches():
    """Within window but different food_id -> no match."""
    g = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    s = make_food_log_entry(
        food_pk_hex="cd" * 16,
        modified_at=datetime(2024, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    assert upsert_match(g, s) is False


def test_missing_modified_at_never_matches():
    """If either side lacks modified_at, conservative answer is no match."""
    g = make_grain_entry(food_id="ab" * 16, modified_at="")
    s = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    assert upsert_match(g, s) is False
    g2 = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    s2 = make_food_log_entry(food_pk_hex="ab" * 16, modified_at=None)
    assert upsert_match(g2, s2) is False


# ── plan_day ─────────────────────────────────────────────────────────────────


def test_plan_day_partitions_correctly():
    """2 archive entries, 1 matches, 1 doesn't -> matched=1, missing=1."""
    g1 = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    g2 = make_grain_entry(food_id="cd" * 16, modified_at="2024-03-15T13:30:00+00:00")
    s1 = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 2, 0, tzinfo=UTC),
    )
    plan = plan_day([g1, g2], [s1])
    assert plan.matched == [UpsertMatch(g1, s1)]
    assert plan.missing == [g2]


def test_plan_day_each_server_entry_consumed_once():
    """If two archive entries could match the same server entry, only the
    first wins (server entry consumed). The second falls into missing.

    This prevents a single server entry from suppressing multiple
    legitimate re-logs.
    """
    g1 = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    g2 = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:01:00+00:00")
    # Only ONE server entry, but both archive entries could match it within ±10m.
    s1 = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 0, 30, tzinfo=UTC),
    )
    plan = plan_day([g1, g2], [s1])
    assert len(plan.matched) == 1
    assert plan.matched[0].grain_entry == g1
    assert plan.missing == [g2]


def test_plan_day_extra_server_entries_left_alone():
    """If the server has entries that don't match any archive entry, they're
    NOT in matched and NOT in missing. They simply stay on the server (§7.4).
    """
    g = make_grain_entry(food_id="ab" * 16, modified_at="2024-03-15T12:00:00+00:00")
    s_archive = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    s_extra = make_food_log_entry(
        food_pk_hex="ef" * 16,
        modified_at=datetime(2024, 3, 15, 18, 0, 0, tzinfo=UTC),
    )
    plan = plan_day([g], [s_archive, s_extra])
    assert len(plan.matched) == 1
    assert plan.missing == []
    # s_extra is not anywhere in the plan — that's the additive-only contract.


def test_food_id_from_food_log_entry_roundtrips():
    """Building a FoodLogEntry with hex 'ab'*16 then reading the hex back works."""
    fle = make_food_log_entry(
        food_pk_hex="ab" * 16,
        modified_at=datetime(2024, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    assert food_id_from_food_log_entry(fle) == "ab" * 16


def test_default_window_is_ten_minutes():
    """Sanity check: the default fuzz really is the 10 minutes the spec calls for."""
    assert DEFAULT_UPSERT_WINDOW.total_seconds() == 600
