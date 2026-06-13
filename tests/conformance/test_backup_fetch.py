"""Conformance tests for the backup fetch primitive (T2).

Covers spec §6 (grain-at-a-time fetch with recursive split-and-retry)
and spec §6.3 (describe-cadence once per UTC day). Every test is
hermetic — fake :class:`LoseIt`-shaped objects with bounded call
counters stand in for the real client.

Spec ambiguity resolution: spec §4.1 names ``created_at`` as the third
key of the canonical entry sort. T4's empirical analysis (see the
module docstring of :mod:`lose_it.backup._fetch`) showed
``FoodLogEntry.created_at`` is **not** a real timestamp — only
``modified_at`` is. We therefore use ``modified_at`` in the sort key
and pin that decision with a dedicated test below.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from lose_it.backup import (
    AccountRef,
    FetchStatus,
    FoodsDoc,
    Grain,
    GrainEntry,
    fetch_grain,
    grain_entry_sort_key,
    to_grain_entry,
    update_food_cache,
    write_foods_file,
)
from lose_it.core.daily import TooMuchData
from lose_it.models import FoodLogEntry

# ── Helpers / fakes ──────────────────────────────────────────────────────────


def _make_fle(*, day_num: int, food_name: str = "N", food_id: str = "f" * 32) -> FoodLogEntry:
    """Build a minimum-viable :class:`FoodLogEntry` for tests.

    The fields with defaults are not exercised; the few we *do* set are
    the ones the fetch primitive (or its sort key) actually consults.
    """
    # Convert the supplied hex string into a 16-byte response list so
    # ``food_id`` property round-trips. The pk_to_hex round-trip lives
    # in the SDK; for these tests we just need *some* unique 16-int
    # vector so ``food_id`` is consistent across calls.
    pk = [(i + sum(ord(c) for c in food_id)) % 256 - 128 for i in range(16)]
    return FoodLogEntry(
        food_category="C",
        food_name=food_name,
        food_brand="B",
        food_pk_response=pk,
        entry_pk_response=pk,
        entry_day_key="",
        context_day_key="",
        day_num=day_num,
        hours_from_gmt=0,
        meal_ordinal=0,
        extra_ordinal=3,
        food_measure_ordinal=0,
        servings=1.0,
        food_identifier_code="",
    )


class FakeLoseIt:
    """A controlled :class:`LoseIt`-shaped fake for the fetch primitive.

    Records every call so a test can assert on call counts and on the
    arguments each call received. Behavior is policy-driven:

    * ``diary_range`` consults ``range_policy`` to decide whether to
      raise :class:`TooMuchData` or return a (day → entries) map.
    * ``diary`` consults ``diary_policy`` for the same.

    Policies are simple callables; tests can either point them at
    pre-built return dictionaries or hand-craft conditional behavior
    (e.g. "fail at month grain, succeed at week").
    """

    def __init__(
        self,
        *,
        range_policy=None,
        diary_policy=None,
    ) -> None:
        self.range_policy = range_policy
        self.diary_policy = diary_policy
        self.range_calls: list[tuple[date, date]] = []
        self.diary_calls: list[date] = []

    def diary_range(self, start: date, end: date) -> dict[date, list[FoodLogEntry]]:
        self.range_calls.append((start, end))
        if self.range_policy is None:
            return {}
        return self.range_policy(start, end)

    def diary(self, when: date) -> list[FoodLogEntry]:
        self.diary_calls.append(when)
        if self.diary_policy is None:
            return []
        return self.diary_policy(when)


def _all_empty_range(start: date, end: date) -> dict[date, list[FoodLogEntry]]:
    """A ``diary_range`` policy: every day in [start, end] is empty."""
    out: dict[date, list[FoodLogEntry]] = {}
    cursor = start
    while cursor <= end:
        out[cursor] = []
        cursor += timedelta(days=1)
    return out


# ── Grain construction ──────────────────────────────────────────────────────


def test_grain_month_constructor_spans_calendar_month():
    """Grain.month(any_day) covers first-of-month .. last-of-month inclusive."""
    g = Grain.month(date(2016, 2, 15))
    assert g.kind == "month"
    assert g.start == date(2016, 2, 1)
    assert g.end == date(2016, 2, 29)  # leap year


def test_grain_week_constructor_is_iso_monday_to_sunday():
    """Grain.week(any_day) returns the ISO-week's Monday..Sunday."""
    # 2016-02-15 is a Monday — same day is the start.
    g = Grain.week(date(2016, 2, 15))
    assert g.kind == "week"
    assert g.start == date(2016, 2, 15)
    assert g.end == date(2016, 2, 21)
    # And a mid-week day rolls back to that same Monday.
    assert Grain.week(date(2016, 2, 17)).start == date(2016, 2, 15)


def test_grain_split_month_into_weeks_covers_the_month():
    """Splitting a month yields ISO-week grains whose union covers it."""
    g = Grain.month(date(2016, 2, 15))
    subs = g.split_one_step()
    assert all(s.kind == "week" for s in subs)
    # Every day of February must appear in some sub-grain's range.
    covered = set()
    for s in subs:
        cursor = s.start
        while cursor <= s.end:
            covered.add(cursor)
            cursor += timedelta(days=1)
    cursor = g.start
    while cursor <= g.end:
        assert cursor in covered, f"{cursor} not covered by any sub-grain"
        cursor += timedelta(days=1)


def test_grain_split_week_into_seven_days():
    """A week splits into exactly 7 day-grains."""
    g = Grain.week(date(2016, 2, 15))
    subs = g.split_one_step()
    assert len(subs) == 7
    assert [s.kind for s in subs] == ["day"] * 7
    assert [s.start for s in subs] == [date(2016, 2, 15 + i) for i in range(7)]


def test_grain_split_day_raises_at_recursion_floor():
    """Day-grain split raises ValueError — the recursion floor of the splitter."""
    with pytest.raises(ValueError):
        Grain.day(date(2016, 2, 15)).split_one_step()


# ── Fetch primitive ─────────────────────────────────────────────────────────


def test_month_grain_one_rpc_on_clean_fetch():
    """Spec §6.1: a clean month-grain backup is 1 RPC, not 28-31."""
    fake = FakeLoseIt(range_policy=_all_empty_range)
    grain = Grain.month(date(2016, 2, 15))
    entries, status = fetch_grain(fake, grain, sleep_seconds=0.0)
    assert status is FetchStatus.fetch
    assert entries == []
    assert len(fake.range_calls) == 1
    assert fake.range_calls[0] == (date(2016, 2, 1), date(2016, 2, 29))
    assert len(fake.diary_calls) == 0


def test_clean_fetch_returns_flattened_entries_in_date_order():
    """Multi-day clean fetch returns entries flattened in chronological order."""

    def policy(start, end):
        return {
            date(2016, 2, 1): [_make_fle(day_num=16832, food_name="A")],
            date(2016, 2, 2): [_make_fle(day_num=16833, food_name="B")],
            date(2016, 2, 3): [],
        }

    fake = FakeLoseIt(range_policy=policy)
    g = Grain(kind="week", start=date(2016, 2, 1), end=date(2016, 2, 3))
    entries, status = fetch_grain(fake, g, sleep_seconds=0.0)
    assert status is FetchStatus.fetch
    assert [e.food_name for e in entries] == ["A", "B"]


def test_oversize_falls_back_to_week_grain():
    """fetch_grain catches TooMuchData on a month, recurses to weeks."""

    def range_policy(start, end):
        span = (end - start).days + 1
        # Month-sized ranges raise; smaller (week-sized) ranges succeed.
        if span > 20:
            raise TooMuchData("month is too big")
        out: dict[date, list[FoodLogEntry]] = {}
        cursor = start
        while cursor <= end:
            out[cursor] = []
            cursor += timedelta(days=1)
        return out

    fake = FakeLoseIt(range_policy=range_policy)
    grain = Grain.month(date(2016, 2, 15))
    entries, status = fetch_grain(fake, grain, sleep_seconds=0.0)
    assert status is FetchStatus.fallback
    assert entries == []
    # One month-level call (that raised) plus N week-level calls.
    week_calls = [(s, e) for s, e in fake.range_calls if (e - s).days + 1 <= 7]
    month_calls = [(s, e) for s, e in fake.range_calls if (e - s).days + 1 > 7]
    assert len(month_calls) == 1
    assert 4 <= len(week_calls) <= 6  # February straddles 4-6 ISO weeks
    # Day-level diary() never fires — week worked.
    assert fake.diary_calls == []


def test_oversize_falls_back_through_week_to_day():
    """When weeks ALSO oversize, the splitter recurses again to day-grain."""

    def range_policy(start, end):
        span = (end - start).days + 1
        # Both month AND week ranges raise; only day-grain
        # (diary_range with start == end) succeeds.
        if span > 1:
            raise TooMuchData("everything bigger than a day is too big")
        return {start: []}

    fake = FakeLoseIt(range_policy=range_policy)
    grain = Grain.month(date(2016, 2, 15))
    entries, status = fetch_grain(fake, grain, sleep_seconds=0.0)
    assert status is FetchStatus.fallback
    assert entries == []
    # The implementation hits diary_range with start == end at the day
    # recursion floor (since that's how Grain.day(d) is shaped). At
    # least 28 day-range calls must have fired to cover February.
    day_calls = [(s, e) for s, e in fake.range_calls if s == e]
    assert len(day_calls) >= 28


def test_day_grain_failure_re_raises():
    """If the splitter hits day-grain TooMuchData, fetch_grain re-raises."""

    def always_too_much(start, end):
        raise TooMuchData("nothing works today")

    fake = FakeLoseIt(range_policy=always_too_much)
    grain = Grain.month(date(2016, 2, 15))
    with pytest.raises(TooMuchData):
        fetch_grain(fake, grain, sleep_seconds=0.0)


def test_chained_split_aborts_on_day_floor():
    """All grains TooMuchData → backup aborts; entries never accumulated."""

    def always_too_much(start, end):
        raise TooMuchData("server is down")

    fake = FakeLoseIt(range_policy=always_too_much)
    grain = Grain.month(date(2016, 2, 15))
    with pytest.raises(TooMuchData):
        fetch_grain(fake, grain, sleep_seconds=0.0)
    # The orchestrator (T6) is responsible for not writing a grain
    # file when this exception escapes; this test pins T2's contract:
    # we don't swallow the exception with a partial result.


# ── Describe-cadence ────────────────────────────────────────────────────────


def _seed_foods_file(
    path: Path,
    *,
    food_id: str = "a" * 32,
    last_described_at: str = "2026-06-12T20:00:00+00:00",
) -> None:
    """Write a foods.toon with one fully-populated row at ``food_id``."""
    from lose_it.backup import FoodCacheEntry

    doc = FoodsDoc(
        account=AccountRef(user_id="12345678", user_name="test.user"),
        foods={
            food_id: FoodCacheEntry(
                food_id=food_id,
                last_described_at=last_described_at,
                first_seen_date=date(2026, 6, 12),
                last_seen_date=date(2026, 6, 12),
                name="Tortilla",
                brand="Carb",
                category="Bread",
                primary_serving={"ordinal": 27, "unit": "serving"},
                cross_class_conversion={"per_serving_g": None, "per_serving_ml": None},
                nutrients_per_serving={"calories": 70.0},
                raw_nutrients_by_ord={"0": 70.0},
            )
        },
    )
    write_foods_file(path, doc)


class FakeDescribeLoseIt:
    """A controlled :class:`LoseIt`-shaped fake exposing only describe_food.

    Counts calls and returns a lightweight stub object whose attributes
    match the duck-typed shape :func:`update_food_cache` consults.
    """

    def __init__(self) -> None:
        self.describe_calls: list[str] = []

    def describe_food(self, food_id: str):
        self.describe_calls.append(food_id)
        return _FakeDescription()


class _FakeServing:
    @staticmethod
    def to_dict() -> dict[str, object]:
        return {"ordinal": 27, "unit": "serving"}


class _FakeCrossClass:
    @staticmethod
    def to_dict() -> dict[str, object]:
        return {"per_serving_g": None, "per_serving_ml": None}


class _FakeDescription:
    """Duck-typed substitute for :class:`FoodDescription` in test stubs."""

    name: ClassVar[str] = "Tortilla v2"
    brand: ClassVar[str] = "Carb v2"
    category: ClassVar[str] = "Bread v2"
    nutrients_per_serving: ClassVar[dict[str, float]] = {"calories": 71.0}
    raw_nutrients_by_ord: ClassVar[dict[str, float]] = {"0": 71.0}
    primary_serving: ClassVar[type[_FakeServing]] = _FakeServing
    cross_class_conversion: ClassVar[type[_FakeCrossClass]] = _FakeCrossClass


def test_describe_cadence_once_per_utc_day(tmp_path):
    """Spec §6.3: same food on the same UTC day → no second describe."""
    foods_path = tmp_path / "foods.toon"
    food_id = "a" * 32
    _seed_foods_file(
        foods_path,
        food_id=food_id,
        last_described_at="2026-06-12T20:00:00+00:00",
    )
    original_bytes = foods_path.read_bytes()
    li = FakeDescribeLoseIt()
    count = update_food_cache(
        li,
        foods_path,
        seen_food_ids=[food_id],
        sleep_seconds=0.0,
        today_utc=date(2026, 6, 12),
    )
    assert count == 0
    assert li.describe_calls == []
    # No-op: file mustn't be rewritten when nothing changed.
    assert foods_path.read_bytes() == original_bytes


def test_describe_cadence_re_describes_next_utc_day(tmp_path):
    """A later UTC date forces a re-describe; the cache row updates."""
    foods_path = tmp_path / "foods.toon"
    food_id = "a" * 32
    _seed_foods_file(
        foods_path,
        food_id=food_id,
        last_described_at="2026-06-12T20:00:00+00:00",
    )
    li = FakeDescribeLoseIt()
    count = update_food_cache(
        li,
        foods_path,
        seen_food_ids=[food_id],
        sleep_seconds=0.0,
        today_utc=date(2026, 6, 13),
    )
    assert count == 1
    assert li.describe_calls == [food_id]
    # The on-disk row's last_described_at is now today's UTC date.
    from lose_it.backup import read_foods_file

    updated = read_foods_file(foods_path)
    assert updated.foods[food_id].last_described_at != "2026-06-12T20:00:00+00:00"
    today_iso_prefix = "2026-06-13"
    assert updated.foods[food_id].last_described_at.startswith(today_iso_prefix)
    # The recaptured SCD fields landed.
    assert updated.foods[food_id].name == "Tortilla v2"


def test_describe_cadence_new_food_id_always_described(tmp_path):
    """A food_id absent from the cache is described regardless of timing."""
    foods_path = tmp_path / "foods.toon"
    seeded = "a" * 32
    _seed_foods_file(foods_path, food_id=seeded)
    li = FakeDescribeLoseIt()
    fresh = "b" * 32
    count = update_food_cache(
        li,
        foods_path,
        seen_food_ids=[seeded, fresh],
        sleep_seconds=0.0,
        today_utc=date(2026, 6, 12),
    )
    # The seeded food was last-described today (per _seed_foods_file's
    # default timestamp) → skipped. The fresh food → described.
    assert count == 1
    assert li.describe_calls == [fresh]


def test_describe_cadence_deduplicates_repeated_ids(tmp_path):
    """The same food_id appearing twice in seen_ids only describes once."""
    foods_path = tmp_path / "foods.toon"
    seeded = "a" * 32
    _seed_foods_file(foods_path, food_id=seeded)
    li = FakeDescribeLoseIt()
    fresh = "b" * 32
    update_food_cache(
        li,
        foods_path,
        seen_food_ids=[fresh, fresh, fresh],
        sleep_seconds=0.0,
        today_utc=date(2026, 6, 12),
    )
    assert li.describe_calls == [fresh]


# ── Sort key (spec §4.1 with the modified_at substitution) ───────────────────


def _bare_grain_entry(
    *,
    day_num: int,
    meal_ordinal: int,
    modified_at: str,
    created_at: str = "1970-02-15T00:00:00+00:00",
) -> GrainEntry:
    """Minimum-viable :class:`GrainEntry` for sort-order tests."""
    return GrainEntry(
        date=date(2016, 2, 15),
        day_num=day_num,
        meal=["breakfast", "lunch", "dinner", "snacks"][meal_ordinal],
        meal_ordinal=meal_ordinal,
        food_id="f" * 32,
        food_name="N",
        food_brand="B",
        food_category="C",
        food_identifier_code="",
        food_measure_ordinal=0,
        food_measure_unit="grams",
        servings=1.0,
        calories=0.0,
        created_at=created_at,
        modified_at=modified_at,
    )


def test_to_grain_entry_uses_modified_at_when_created_at_is_bogus():
    """The canonical sort tuple uses modified_at, not created_at.

    Spec §4.1 names ``created_at`` but T4's empirical analysis showed
    FLE.f4 is not a real timestamp. The two rows below share day_num,
    meal_ordinal, and a 1970-clustered ``created_at`` (the bogus
    value). Only ``modified_at`` distinguishes them — and that is what
    must drive the sort.
    """
    early = _bare_grain_entry(
        day_num=16832,
        meal_ordinal=1,
        modified_at="2024-12-15T09:00:00+00:00",
        created_at="1970-02-15T00:00:00+00:00",
    )
    late = _bare_grain_entry(
        day_num=16832,
        meal_ordinal=1,
        modified_at="2024-12-15T18:00:00+00:00",
        created_at="1970-02-15T00:00:00+00:00",
    )
    # Hand them to the sort key in the wrong order.
    in_order = sorted([late, early], key=grain_entry_sort_key)
    assert in_order[0] is early
    assert in_order[1] is late
    # And the sort key's third component is modified_at, not created_at.
    assert grain_entry_sort_key(early)[2] == "2024-12-15T09:00:00+00:00"


def test_grain_entry_sort_key_primary_keys_still_dominate():
    """day_num and meal_ordinal still take precedence over modified_at."""
    later_day = _bare_grain_entry(
        day_num=16833,
        meal_ordinal=0,
        modified_at="2024-01-01T00:00:00+00:00",
    )
    earlier_day = _bare_grain_entry(
        day_num=16832,
        meal_ordinal=3,
        modified_at="2024-12-31T23:59:59+00:00",
    )
    in_order = sorted([later_day, earlier_day], key=grain_entry_sort_key)
    assert in_order[0] is earlier_day  # day_num 16832 < 16833
    assert in_order[1] is later_day


# ── to_grain_entry projection ───────────────────────────────────────────────


def test_to_grain_entry_carries_iso_timestamps():
    """to_grain_entry serializes datetimes as ISO 8601 strings."""
    fle = _make_fle(day_num=16832)
    fle.created_at = datetime(2016, 2, 15, 12, 0, 0, tzinfo=UTC)
    fle.modified_at = datetime(2016, 2, 15, 13, 0, 0, tzinfo=UTC)
    entry = to_grain_entry(
        fle,
        entry_date=date(2016, 2, 15),
        ingest_ts="2026-06-12T20:00:00+00:00",
    )
    assert entry.created_at == "2016-02-15T12:00:00+00:00"
    assert entry.modified_at == "2016-02-15T13:00:00+00:00"
    assert entry.ingest_ts == "2026-06-12T20:00:00+00:00"


def test_to_grain_entry_fills_food_name_from_cache_when_missing():
    """If the FLE has no food_name, fall back to the cache row's name."""
    fle = _make_fle(day_num=16832, food_name="")
    fle.food_brand = ""  # _make_fle sets a default "B" — null it for this test.
    fle.food_category = ""
    entry = to_grain_entry(
        fle,
        entry_date=date(2016, 2, 15),
        food_description={
            "name": "cached name",
            "brand": "cached brand",
            "category": "cached category",
        },
        ingest_ts="2026-06-12T20:00:00+00:00",
    )
    assert entry.food_name == "cached name"
    assert entry.food_brand == "cached brand"
    assert entry.food_category == "cached category"


def test_to_grain_entry_handles_missing_timestamps():
    """A FLE without created_at/modified_at produces empty-string fields."""
    fle = _make_fle(day_num=16832)
    # The default _make_fle leaves both timestamps as None.
    entry = to_grain_entry(
        fle,
        entry_date=date(2016, 2, 15),
        ingest_ts="2026-06-12T20:00:00+00:00",
    )
    assert entry.created_at == ""
    assert entry.modified_at == ""
