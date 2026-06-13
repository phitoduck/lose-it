"""Conformance tests for safe-mode restore (Track T8).

Hermetic — every test wires a structural :class:`FakeLoseIt` (from the
T6 orchestrator suite) into :func:`restore_backup_safe` and asserts on
the per-day plan decisions + the eventual ``log_food`` call shape.

Spec coverage (per ``docs/backup-spec.md``):

* §3.2 safe-mode CLI surface (the per-grain ``upsert/present/empty``
  trio that :class:`SafeRestoreGrainReport` carries).
* §7.1 — entry-level upsert by ``(food_id, modified_at ± window)``.
* §7.4 — restore is purely additive: server-only entries are left
  alone and never enumerated as "missing".
* §8 — ``strict_account`` failsafe.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from lose_it.backup import (
    AccountRef,
    FoodsDoc,
    GrainBounds,
    GrainDoc,
    GrainEntry,
    SafeRestoreGrainReport,
    restore_backup_safe,
    write_foods_file,
    write_grain_file,
)
from lose_it.core._ids import pk_to_hex
from lose_it.models import FoodLogEntry

# Re-use the orchestrator suite's FakeLoseIt so we have a single source
# of truth for the structural client double.
from tests.conformance.test_backup_orchestrator import FakeLoseIt

# ── Helpers ─────────────────────────────────────────────────────────────────


def _account() -> AccountRef:
    return AccountRef(user_id="12345678", user_name="test.user")


def _hex_to_pk(hex32: str) -> list[int]:
    """Tiny inverse helper for :func:`pk_to_hex` used by the fakes below.

    The grain file's ``food_id`` is hex; ``FoodLogEntry.food_pk_response``
    is the raw 16-int form. We bridge via ``bytes.fromhex`` + signed
    re-mapping to mirror the SDK's wire shape.
    """
    raw = bytes.fromhex(hex32)
    # The wire shape is signed bytes — values > 127 become negative.
    return [b if b < 128 else b - 256 for b in raw]


def _grain_entry(
    *,
    food_id: str,
    day: date,
    meal_ordinal: int = 3,
    servings: float = 0.1,
    modified_at: str = "2016-02-15T12:00:00+00:00",
) -> GrainEntry:
    """Minimum-viable :class:`GrainEntry` for safe-mode restore tests."""
    return GrainEntry(
        date=day,
        day_num=16832,
        meal=["breakfast", "lunch", "dinner", "snacks"][meal_ordinal],
        meal_ordinal=meal_ordinal,
        food_id=food_id,
        food_name="N",
        food_brand="B",
        food_category="C",
        food_identifier_code="",
        food_measure_ordinal=27,
        food_measure_unit="serving",
        servings=servings,
        calories=None,
        created_at="2016-02-15T12:00:00+00:00",
        modified_at=modified_at,
        ingest_ts="2026-06-12T20:00:00+00:00",
    )


def _server_fle(
    *,
    food_id_hex: str,
    modified_at: datetime,
) -> FoodLogEntry:
    """Server-side :class:`FoodLogEntry` carrying ``modified_at`` + a PK."""
    return FoodLogEntry(
        food_category="C",
        food_name="N",
        food_brand="B",
        food_pk_response=_hex_to_pk(food_id_hex),
        entry_pk_response=[2] * 16,
        entry_day_key="",
        context_day_key="",
        day_num=16832,
        hours_from_gmt=0,
        meal_ordinal=3,
        extra_ordinal=3,
        food_measure_ordinal=27,
        servings=1.0,
        food_identifier_code="",
        created_at=modified_at,
        modified_at=modified_at,
    )


def _seed_grain_file(
    path: Path,
    *,
    entries: list[GrainEntry],
    grain_kind: str = "month",
    start: date = date(2016, 2, 1),
    end: date = date(2016, 2, 29),
    account: AccountRef | None = None,
) -> None:
    doc = GrainDoc(
        account=account or _account(),
        grain=GrainBounds(kind=grain_kind, start=start, end=end),
        generated_at="2026-06-12T20:00:00+00:00",
        entries=entries,
    )
    write_grain_file(path, doc)


def _seed_foods_file(root: Path) -> None:
    write_foods_file(root / "foods.toon", FoodsDoc(account=_account(), foods={}))


# ── Tests ───────────────────────────────────────────────────────────────────


def test_safe_restore_logs_missing_entries(tmp_path):
    """Archive has 2 entries on Feb 15, server empty → both logged.

    Pins spec §7.1's flowchart: for every day with archive entries,
    every entry missing on the server fires one ``log_food`` call.
    """
    root = tmp_path / "bkup"
    food_a = "a" * 32
    food_b = "b" * 32
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[
            _grain_entry(food_id=food_a, day=date(2016, 2, 15)),
            _grain_entry(food_id=food_b, day=date(2016, 2, 15), servings=0.2),
        ],
    )
    _seed_foods_file(root)

    li = FakeLoseIt()  # default diary policy → empty
    reports: list[SafeRestoreGrainReport] = []
    summary = restore_backup_safe(
        li,
        root=root,
        grain="month",
        sleep_seconds=0.0,
        progress=reports.append,
    )

    # Two log_food calls fired, carrying the spec §4.4 minimum payload.
    assert len(li.log_calls) == 2
    assert {c["food"] for c in li.log_calls} == {food_a, food_b}
    assert all(c["meal"] == 3 for c in li.log_calls)
    assert all(c["when"] == date(2016, 2, 15) for c in li.log_calls)

    # One server read per day-with-entries; archive only touches Feb 15.
    assert li.diary_calls == [date(2016, 2, 15)]

    # Summary rolls up the per-day counters.
    assert summary.grains_scanned == 1
    assert summary.grains_restored == 1
    assert summary.entries_logged == 2
    assert summary.entries_already_present == 0
    assert summary.days_scanned == 1
    assert summary.days_upserted == 1
    assert summary.days_fully_present == 0

    # The per-grain report mirrors the summary at grain granularity.
    assert len(reports) == 1
    r = reports[0]
    assert r.days_with_entries == 1
    assert r.days_upserted == 1
    assert r.days_present == 0
    assert r.entries_logged == 2
    assert r.entries_present == 0


def test_safe_restore_idempotent_second_run(tmp_path):
    """Run twice; second run logs 0 (entries match via modified_at).

    Pins spec §7.1's idempotency contract: the safe-mode restore is the
    only mode that's "truly idempotent at the entry level" (spec §7.3).
    """
    root = tmp_path / "bkup"
    food_id = "a" * 32
    modified_iso = "2016-02-15T12:00:00+00:00"
    modified_dt = datetime.fromisoformat(modified_iso)
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[
            _grain_entry(food_id=food_id, day=date(2016, 2, 15), modified_at=modified_iso),
        ],
    )

    # First run: server is empty → log fires.
    li = FakeLoseIt()
    first = restore_backup_safe(li, root=root, sleep_seconds=0.0)
    assert first.entries_logged == 1
    assert first.entries_already_present == 0

    # Second run: server reports the same food_id with matching
    # modified_at → upsert match succeeds, zero logs.
    server_fle = _server_fle(food_id_hex=food_id, modified_at=modified_dt)

    def diary_policy(when: date) -> list[FoodLogEntry]:
        return [server_fle] if when == date(2016, 2, 15) else []

    li2 = FakeLoseIt(diary_policy=diary_policy)
    second = restore_backup_safe(li2, root=root, sleep_seconds=0.0)
    assert li2.log_calls == []
    assert second.entries_logged == 0
    assert second.entries_already_present == 1
    assert second.days_fully_present == 1
    assert second.days_upserted == 0


def test_safe_restore_outside_window_is_missing(tmp_path):
    """Server entry has modified_at outside ±10m → archive entry counted
    as missing → logged.

    Pins the upsert window logic (spec §7.1's "drift outside the ±10
    minute window counts as missing").
    """
    root = tmp_path / "bkup"
    food_id = "a" * 32
    archive_iso = "2016-02-15T12:00:00+00:00"
    archive_dt = datetime.fromisoformat(archive_iso)
    # Server reports the same food id but 11 minutes later — outside ±10m.
    server_dt = archive_dt + timedelta(minutes=11)
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[
            _grain_entry(food_id=food_id, day=date(2016, 2, 15), modified_at=archive_iso),
        ],
    )

    def diary_policy(when: date) -> list[FoodLogEntry]:
        if when == date(2016, 2, 15):
            return [_server_fle(food_id_hex=food_id, modified_at=server_dt)]
        return []

    li = FakeLoseIt(diary_policy=diary_policy)
    summary = restore_backup_safe(li, root=root, sleep_seconds=0.0)

    # The archive entry doesn't match the (off-window) server entry → logged.
    assert summary.entries_logged == 1
    assert summary.entries_already_present == 0
    assert summary.days_upserted == 1
    assert li.log_calls[0]["food"] == food_id


def test_safe_restore_extra_server_entries_left_alone(tmp_path):
    """Server has an entry the archive doesn't → restore additive, no delete.

    Pins spec §7.4 ("Why no delete path"). The orchestrator must not
    enumerate the server-only entry as missing and must not invoke
    anything resembling a delete on the fake.
    """
    root = tmp_path / "bkup"
    food_in_archive = "a" * 32
    food_only_on_server = "b" * 32
    server_dt = datetime(2016, 2, 15, 12, 0, tzinfo=UTC)

    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[
            _grain_entry(
                food_id=food_in_archive,
                day=date(2016, 2, 15),
                modified_at=server_dt.isoformat(),
            ),
        ],
    )

    def diary_policy(when: date) -> list[FoodLogEntry]:
        if when == date(2016, 2, 15):
            return [
                _server_fle(food_id_hex=food_in_archive, modified_at=server_dt),
                _server_fle(food_id_hex=food_only_on_server, modified_at=server_dt),
            ]
        return []

    li = FakeLoseIt(diary_policy=diary_policy)
    summary = restore_backup_safe(li, root=root, sleep_seconds=0.0)

    # Archive entry matched server entry → zero logs.
    assert li.log_calls == []
    assert summary.entries_logged == 0
    assert summary.entries_already_present == 1
    # And the server-only entry is silently ignored — neither in
    # ``missing`` nor in any counter the orchestrator surfaces.
    assert summary.days_fully_present == 1


def test_safe_restore_dry_run_does_not_log(tmp_path):
    """``dry_run=True`` reads server diaries but issues no ``log_food``."""
    root = tmp_path / "bkup"
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[_grain_entry(food_id="a" * 32, day=date(2016, 2, 15))],
    )

    li = FakeLoseIt()
    summary = restore_backup_safe(
        li,
        root=root,
        sleep_seconds=0.0,
        dry_run=True,
    )

    # Read pass happened.
    assert li.diary_calls == [date(2016, 2, 15)]
    # But no logs fired.
    assert li.log_calls == []
    # Summary still counts the grain.
    assert summary.grains_restored == 1
    assert summary.entries_logged == 0


def test_safe_restore_strict_account_refuses_wrong_user(tmp_path):
    """Spec §8: refuse to restore a grain file pinned to a different user."""
    root = tmp_path / "bkup"
    other = AccountRef(user_id="99", user_name="other.user")
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[_grain_entry(food_id="a" * 32, day=date(2016, 2, 15))],
        account=other,
    )

    li = FakeLoseIt()
    with pytest.raises(ValueError, match="strict_account"):
        restore_backup_safe(li, root=root, sleep_seconds=0.0)


def test_safe_restore_pk_round_trip_sanity():
    """Sanity: our ``_hex_to_pk`` helper round-trips through
    :func:`pk_to_hex` so the fake's server entries actually advertise
    the food_id the upsert key compares against.
    """
    hex32 = "0123456789abcdef" * 2
    assert pk_to_hex(_hex_to_pk(hex32)) == hex32
