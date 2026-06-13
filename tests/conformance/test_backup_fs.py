"""Conformance tests for the backup file-format library (T1).

Covers spec §4 (file shape) and impl-plan §6/T1 (BDDs as unit-level
assertions). Pure hermetic — no network. The dataclasses and helpers
under test live in :mod:`lose_it.backup._fs`.
"""

from __future__ import annotations

from datetime import date

import pytest

from lose_it.backup import (
    SCHEMA_VERSION,
    AccountRef,
    FoodCacheEntry,
    FoodsDoc,
    GrainBounds,
    GrainDoc,
    GrainEntry,
    IndexDoc,
    SchemaVersionMismatch,
    atomic_write_text,
    read_foods_file,
    read_grain_file,
    read_index_file,
    same_account,
    write_foods_file,
    write_grain_file,
    write_index_file,
)


def _make_entry(*, day_num: int, meal_ordinal: int, created_at: str) -> GrainEntry:
    """Build a fully-populated GrainEntry with the three sort-key fields set."""
    return GrainEntry(
        date=date(2016, 2, 15),
        day_num=day_num,
        meal=["breakfast", "lunch", "dinner", "snacks"][meal_ordinal],
        meal_ordinal=meal_ordinal,
        food_id=f"food-{day_num}-{meal_ordinal}",
        food_name="Test Food",
        food_brand="Test",
        food_category="Snack",
        food_identifier_code="ABC",
        food_measure_ordinal=27,
        food_measure_unit="serving",
        servings=1.0,
        calories=70.0,
        nutrients={"0": 70.0},
        nutrients_by_label={"calories": 70.0},
        entry_pk_response=[1, 2, 3],
        food_pk_response=[4, 5, 6],
        entry_day_key="Z66oWlo",
        context_day_key="Z66oWlo",
        hours_from_gmt=-6,
        created_at=created_at,
        modified_at=created_at,
        ingest_ts="2026-06-12T20:00:01+00:00",
    )


def test_atomic_write_leaves_no_tmp(tmp_path):
    """tmp -> fsync -> os.replace; nothing matching *.tmp* must linger."""
    path = tmp_path / "sub" / "file.toon"
    atomic_write_text(path, "hello: world\n")
    assert path.read_text() == "hello: world\n"
    # No .tmp files anywhere under tmp_path.
    assert list(tmp_path.rglob("*.tmp*")) == []


def test_atomic_write_creates_missing_parent_dirs(tmp_path):
    """The helper makes parents on demand — spec §6.4."""
    path = tmp_path / "a" / "b" / "c" / "file.toon"
    atomic_write_text(path, "x: 1\n")
    assert path.exists()


def test_grain_round_trip(tmp_path):
    """Spec §4.1: GrainDoc -> file -> GrainDoc preserves everything."""
    doc = GrainDoc(
        account=AccountRef(user_id="53539329", user_name="you@example.com"),
        grain=GrainBounds(kind="month", start=date(2016, 2, 1), end=date(2016, 2, 29)),
        generated_at="2026-06-12T20:00:00+00:00",
        entries=[
            GrainEntry(
                date=date(2016, 2, 15),
                day_num=6435,
                meal="snacks",
                meal_ordinal=3,
                food_id="5c7218603fd35a86bc4fac771a54560d",
                food_name="Tortilla",
                food_brand="Mission",
                food_category="Tortilla",
                food_identifier_code="DoP_mj",
                food_measure_ordinal=27,
                food_measure_unit="serving",
                servings=1.0,
                calories=70.0,
                nutrients={"0": 70.0, "9": 300.0},
                nutrients_by_label={"calories": 70.0, "sodium_mg": 300.0},
                entry_pk_response=[-2, 99, 41, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                food_pk_response=[92, 114, 24, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                entry_day_key="Z66oWlo",
                context_day_key="Z66oWlo",
                hours_from_gmt=-6,
                created_at="2016-02-15T12:34:08+00:00",
                modified_at="2016-02-15T12:34:08+00:00",
                ingest_ts="2026-06-12T20:00:01+00:00",
            ),
        ],
    )
    p = tmp_path / "2016" / "02.toon"
    write_grain_file(p, doc)
    loaded = read_grain_file(p)
    assert loaded == doc


def test_grain_file_top_level_keys_in_order(tmp_path):
    """Spec §4.1 spells out the key order. The CLI BDDs check this."""
    doc = GrainDoc(
        account=AccountRef("12345678", "test.user"),
        grain=GrainBounds("month", date(2016, 2, 1), date(2016, 2, 29)),
        generated_at="2026-06-12T20:00:00+00:00",
    )
    p = tmp_path / "f.toon"
    write_grain_file(p, doc)
    text = p.read_text()
    head = text.splitlines()[:10]
    assert any(line.startswith("schema_version:") for line in head)
    idx_schema = next(i for i, line in enumerate(head) if line.startswith("schema_version:"))
    idx_account = next(i for i, line in enumerate(head) if line.startswith("account:"))
    idx_grain = next(i for i, line in enumerate(head) if line.startswith("grain:"))
    idx_generated = next(i for i, line in enumerate(head) if line.startswith("generated_at:"))
    assert idx_schema < idx_account < idx_grain < idx_generated


def test_grain_file_empty_entries_renders_zero_count(tmp_path):
    """The §4.1 BDD asserts an empty grain file has ``entries[0]:``."""
    doc = GrainDoc(
        account=AccountRef("12345678", "test.user"),
        grain=GrainBounds("month", date(2016, 2, 1), date(2016, 2, 29)),
        generated_at="2026-06-12T20:00:00+00:00",
    )
    p = tmp_path / "f.toon"
    write_grain_file(p, doc)
    text = p.read_text()
    assert "entries[0]:" in text


def test_schema_version_mismatch_refuses(tmp_path):
    """A file with schema_version: 2 raises SchemaVersionMismatch on read."""
    p = tmp_path / "future.toon"
    p.write_text("schema_version: 2\naccount:\n  user_id: 0\n  user_name: x\n")
    with pytest.raises(SchemaVersionMismatch):
        read_grain_file(p)
    # File untouched.
    assert "schema_version: 2" in p.read_text()


def test_schema_version_mismatch_foods_and_index(tmp_path):
    """Same guard fires for foods.toon and index.toon."""
    foods = tmp_path / "foods.toon"
    foods.write_text("schema_version: 2\naccount:\n  user_id: 0\n  user_name: x\n")
    with pytest.raises(SchemaVersionMismatch):
        read_foods_file(foods)
    idx = tmp_path / "index.toon"
    idx.write_text(
        "schema_version: 2\n"
        "account:\n  user_id: 0\n  user_name: x\n"
        "grain: month\n"
        "discovered_earliest_day: 2016-02-15\n"
        "discovered_at: 2026-06-12T20:00:00+00:00\n"
    )
    with pytest.raises(SchemaVersionMismatch):
        read_index_file(idx)


def test_writer_refuses_future_schema_version(tmp_path):
    """A caller fabricating a doc with schema_version=2 can't write it."""
    doc = GrainDoc(
        account=AccountRef("12345678", "test.user"),
        grain=GrainBounds("month", date(2016, 2, 1), date(2016, 2, 29)),
        generated_at="2026-06-12T20:00:00+00:00",
        schema_version=SCHEMA_VERSION + 1,
    )
    with pytest.raises(SchemaVersionMismatch):
        write_grain_file(tmp_path / "f.toon", doc)


def test_foods_round_trip(tmp_path):
    doc = FoodsDoc(
        account=AccountRef("12345678", "test.user"),
        foods={
            "abc": FoodCacheEntry(
                food_id="abc",
                last_described_at="2026-06-12T20:00:04+00:00",
                first_seen_date=date(2019, 8, 14),
                last_seen_date=date(2026, 6, 12),
                name="Test Food",
                brand="Test",
                category="Snack",
                primary_serving={"ordinal": 27, "unit": "serving"},
                cross_class_conversion={"per_serving_g": None, "per_serving_ml": None},
                nutrients_per_serving={"calories": 70.0},
                raw_nutrients_by_ord={"0": 70.0},
            ),
        },
    )
    p = tmp_path / "foods.toon"
    write_foods_file(p, doc)
    assert read_foods_file(p) == doc


def test_index_round_trip(tmp_path):
    doc = IndexDoc(
        account=AccountRef("12345678", "test.user"),
        grain="month",
        discovered_earliest_day=date(2019, 8, 14),
        discovered_at="2026-06-12T20:00:00+00:00",
    )
    p = tmp_path / "index.toon"
    write_index_file(p, doc)
    assert read_index_file(p) == doc


def test_index_round_trip_with_null_earliest_day(tmp_path):
    """An account with no entries ever — spec §5.3 "no entries ever"."""
    doc = IndexDoc(
        account=AccountRef("12345678", "test.user"),
        grain="month",
        discovered_earliest_day=None,
        discovered_at="2026-06-12T20:00:00+00:00",
    )
    p = tmp_path / "index.toon"
    write_index_file(p, doc)
    assert read_index_file(p) == doc


def test_entries_sorted_on_write(tmp_path):
    """Spec §4.1: ordered (day_num asc, meal_ordinal asc, created_at asc).

    We build a doc with entries in REVERSE canonical order and confirm
    the file writes them in CANONICAL order.
    """
    # Three entries that exercise each tie-breaker level:
    # - smaller day_num wins over larger
    # - within same day, smaller meal_ordinal wins
    # - within same (day_num, meal_ordinal), earlier created_at wins
    e_first = _make_entry(day_num=6435, meal_ordinal=0, created_at="2016-02-15T07:00:00+00:00")
    e_second = _make_entry(day_num=6435, meal_ordinal=3, created_at="2016-02-15T07:00:00+00:00")
    e_third = _make_entry(day_num=6435, meal_ordinal=3, created_at="2016-02-15T20:00:00+00:00")
    e_fourth = _make_entry(day_num=6436, meal_ordinal=0, created_at="2016-02-16T07:00:00+00:00")
    doc = GrainDoc(
        account=AccountRef("12345678", "test.user"),
        grain=GrainBounds("month", date(2016, 2, 1), date(2016, 2, 29)),
        generated_at="2026-06-12T20:00:00+00:00",
        # Reverse order on purpose.
        entries=[e_fourth, e_third, e_second, e_first],
    )
    p = tmp_path / "f.toon"
    write_grain_file(p, doc)
    loaded = read_grain_file(p)
    assert loaded.entries == [e_first, e_second, e_third, e_fourth]


def test_strict_account_guard():
    """``same_account`` helper returns the expected truthiness."""
    a = AccountRef("12345678", "test.user")
    b = AccountRef("99999999", "other.user")
    assert same_account(a, a) is True
    assert same_account(a, b) is False
    # And the dataclass equality is the same.
    assert a != b
    assert a == AccountRef("12345678", "test.user")
