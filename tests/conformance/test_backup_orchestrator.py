"""Conformance tests for the backup orchestrator + cheap-mode restore (T6).

Hermetic — every test uses a structural ``FakeLoseIt`` double that
records calls so we can assert on RPC counts + per-grain decisions.
No network, no real :class:`lose_it.LoseIt` construction.

Spec coverage (per docs/backup-spec.md):

* §3.1 ``backup``: resume / fetch / dry-run, discovery cache.
* §3.2 ``restore-backup``: cheap-mode skip on first non-empty day.
* §4.1 grain-file shape (verified end-to-end through ``write_grain_file``).
* §7.2 cheap-mode early-exit per grain.
* §8 strict-account guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from lose_it.backup import (
    AccountRef,
    BackupSummary,
    CheapRestoreGrainReport,
    FetchStatus,
    FoodsDoc,
    Grain,
    GrainBounds,
    GrainDoc,
    GrainEntry,
    GrainReport,
    IndexDoc,
    RestoreSummary,
    backup,
    read_grain_file,
    read_index_file,
    restore_backup_cheap,
    write_foods_file,
    write_grain_file,
    write_index_file,
)
from lose_it.backup._orchestrator import _enumerate_grains, _grain_file_path
from lose_it.models import FoodLogEntry

# ── Fakes ───────────────────────────────────────────────────────────────────


@dataclass
class _FakeConfig:
    """Just enough surface for ``_account_from_client``."""

    user_id: str
    user_name: str


class FakeLoseIt:
    """Structural :class:`lose_it.LoseIt` double for orchestrator tests.

    Exposes the four methods the orchestrator consumes (``diary``,
    ``diary_range``, ``describe_food``, ``log_food``) plus a
    ``.config`` carrying ``user_id`` / ``user_name``. Every call is
    recorded so tests can assert on RPC count + arguments.

    ``policies`` lets a test pin per-method behavior with a callable
    that returns the canned payload; missing policies default to
    "empty everything" so the bare-bones path is one-liner-testable.
    """

    def __init__(
        self,
        *,
        user_id: str = "12345678",
        user_name: str = "test.user",
        diary_policy=None,
        diary_range_policy=None,
        describe_policy=None,
        log_policy=None,
    ) -> None:
        self.config = _FakeConfig(user_id=user_id, user_name=user_name)
        self.diary_calls: list[date] = []
        self.diary_range_calls: list[tuple[date, date]] = []
        self.describe_calls: list[str] = []
        self.log_calls: list[dict[str, Any]] = []
        self._diary_policy = diary_policy
        self._diary_range_policy = diary_range_policy
        self._describe_policy = describe_policy
        self._log_policy = log_policy

    # ── SDK surface ─────────────────────────────────────────────────────

    def diary(self, when: date) -> list[FoodLogEntry]:
        self.diary_calls.append(when)
        if self._diary_policy is None:
            return []
        return self._diary_policy(when)

    def diary_range(self, start: date, end: date) -> dict[date, list[FoodLogEntry]]:
        self.diary_range_calls.append((start, end))
        if self._diary_range_policy is None:
            out: dict[date, list[FoodLogEntry]] = {}
            cur = start
            while cur <= end:
                out[cur] = []
                cur = cur + timedelta(days=1)
            return out
        return self._diary_range_policy(start, end)

    def describe_food(self, food_id: str):
        self.describe_calls.append(food_id)
        if self._describe_policy is None:
            return _FakeDescription()
        return self._describe_policy(food_id)

    def log_food(
        self,
        food: Any,
        meal: Any = 0,
        servings: float = 1.0,
        *,
        when: date | None = None,
        **kwargs: Any,
    ) -> Any:
        self.log_calls.append(
            {"food": food, "meal": meal, "servings": servings, "when": when, **kwargs}
        )
        if self._log_policy is None:
            return None
        return self._log_policy(food=food, meal=meal, servings=servings, when=when, **kwargs)


class _FakeServing:
    @staticmethod
    def to_dict() -> dict[str, Any]:
        return {"ordinal": 27, "unit": "serving"}


class _FakeCross:
    @staticmethod
    def to_dict() -> dict[str, Any]:
        return {"per_serving_g": None, "per_serving_ml": None}


class _FakeDescription:
    """Duck-typed substitute for :class:`~lose_it.models.FoodDescription`."""

    def __init__(
        self,
        *,
        name: str = "Test Food",
        brand: str = "Test Brand",
        category: str = "Test Cat",
    ) -> None:
        self.name = name
        self.brand = brand
        self.category = category
        self.primary_serving = _FakeServing
        self.cross_class_conversion = _FakeCross
        self.nutrients_per_serving = {"calories": 100.0}
        self.raw_nutrients_by_ord = {"0": 100.0}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _account() -> AccountRef:
    return AccountRef(user_id="12345678", user_name="test.user")


def _seed_grain_file(
    path: Path,
    *,
    grain_kind: str,
    start: date,
    end: date,
    account: AccountRef | None = None,
    entries: list[GrainEntry] | None = None,
) -> None:
    """Write a valid, parseable grain file at ``path``.

    Used by resume / restore-strict-account tests to set up the
    "already-on-disk" precondition.
    """
    doc = GrainDoc(
        account=account or _account(),
        grain=GrainBounds(kind=grain_kind, start=start, end=end),
        generated_at="2026-06-12T20:00:00+00:00",
        entries=entries or [],
    )
    write_grain_file(path, doc)


def _seed_foods_file(root: Path, account: AccountRef | None = None) -> None:
    """Write an empty ``foods.toon`` bound to ``account`` at ``root``."""
    doc = FoodsDoc(account=account or _account(), foods={})
    write_foods_file(root / "foods.toon", doc)


def _grain_entry_for_restore(
    *,
    food_id: str,
    day: date,
    meal_ordinal: int = 3,
    servings: float = 0.1,
) -> GrainEntry:
    """Minimum-viable entry for cheap-mode restore tests.

    Carries the four fields the spec §4.4 re-log path consumes:
    ``food_id``, ``meal_ordinal``, ``servings``, ``date``.
    """
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
        modified_at="2016-02-15T12:00:00+00:00",
        ingest_ts="2026-06-12T20:00:00+00:00",
    )


# ── Backup tests ────────────────────────────────────────────────────────────


def test_resume_skips_complete_grain(tmp_path):
    """Spec §3.1: existing grain file → skip, zero RPCs.

    Precondition: a valid 2016/02.toon already on disk. Run backup
    over the same range. Expect: no ``diary_range`` calls, summary
    reports the grain as skipped.
    """
    root = tmp_path / "bkup"
    _seed_grain_file(
        root / "2016" / "02.toon",
        grain_kind="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
    )
    _seed_foods_file(root)

    li = FakeLoseIt()
    reports: list[GrainReport] = []
    summary = backup(
        li,
        root=root,
        grain="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
        sleep_seconds=0.0,
        progress=reports.append,
    )

    # Zero fetch RPCs.
    assert li.diary_range_calls == []
    assert li.diary_calls == []
    # Summary marks the grain as skipped.
    assert isinstance(summary, BackupSummary)
    assert summary.months_total == 1
    assert summary.months_skipped == 1
    assert summary.months_fetched == 0
    # The progress callback fired with a skip report.
    assert len(reports) == 1
    assert reports[0].status is FetchStatus.skip


def test_single_day_backup_writes_expected_file(tmp_path):
    """End-to-end: ``--start 2016-02-15 --end 2016-02-15 --grain day``
    writes ``<root>/2016/02/15.toon`` with the expected :class:`GrainDoc`
    shape (spec §3.1 BDD scenario).
    """
    root = tmp_path / "bkup"

    li = FakeLoseIt()
    summary = backup(
        li,
        root=root,
        grain="day",
        start=date(2016, 2, 15),
        end=date(2016, 2, 15),
        sleep_seconds=0.0,
    )

    # File at the spec §2 layout location.
    day_file = root / "2016" / "02" / "15.toon"
    assert day_file.exists()
    doc = read_grain_file(day_file)
    # Bounds match.
    assert doc.grain.kind == "day"
    assert doc.grain.start == date(2016, 2, 15)
    assert doc.grain.end == date(2016, 2, 15)
    # Account pinned to the running user.
    assert doc.account == _account()
    # Empty day → empty entries[].
    assert doc.entries == []
    # foods.toon was created (bootstrap).
    assert (root / "foods.toon").exists()
    # Summary counts the grain as fetched.
    assert summary.months_total == 1
    assert summary.months_fetched == 1


def test_dry_run_writes_nothing(tmp_path):
    """``dry_run=True`` → no files on disk, no fetch RPCs.

    The discovery probe still runs (so the cost estimate is accurate)
    but ``index.toon`` is not persisted.
    """
    root = tmp_path / "bkup"

    li = FakeLoseIt()
    summary = backup(
        li,
        root=root,
        grain="month",
        start=date(2016, 2, 1),  # pinned, so discovery doesn't run
        end=date(2016, 2, 29),
        sleep_seconds=0.0,
        dry_run=True,
    )

    # No grain file written.
    assert not (root / "2016" / "02.toon").exists()
    # No foods.toon either.
    assert not (root / "foods.toon").exists()
    # No fetch RPCs.
    assert li.diary_range_calls == []
    assert li.diary_calls == []
    # Summary still counted the grain.
    assert summary.months_total == 1
    assert summary.months_fetched == 1


def test_discovery_writes_index_toon_on_first_run(tmp_path):
    """First run with ``start=None`` → discovery runs, ``index.toon`` written.

    The fake reports a single entry on 2019-08-14 so the discovery
    probe lands on a known earliest day (mirroring the
    ``test_finds_august_2019_earliest_for_typical_profile`` setup in
    the T3 conformance suite).
    """
    root = tmp_path / "bkup"

    def diary_range_policy(start: date, end: date) -> dict[date, list[FoodLogEntry]]:
        out: dict[date, list[FoodLogEntry]] = {}
        cur = start
        while cur <= end:
            entries = [_fake_fle()] if cur == date(2019, 8, 14) else []
            out[cur] = entries
            cur = cur + timedelta(days=1)
        return out

    def diary_policy(when: date) -> list[FoodLogEntry]:
        return [_fake_fle()] if when == date(2019, 8, 14) else []

    li = FakeLoseIt(
        diary_range_policy=diary_range_policy,
        diary_policy=diary_policy,
    )
    # Use a narrow end-date so we don't trigger 80+ grain writes.
    summary = backup(
        li,
        root=root,
        grain="month",
        start=None,  # → discovery
        end=date(2019, 9, 30),
        today=date(2019, 9, 30),
        sleep_seconds=0.0,
    )

    # index.toon was written.
    index_path = root / "index.toon"
    assert index_path.exists()
    idx = read_index_file(index_path)
    assert idx.discovered_earliest_day == date(2019, 8, 14)
    assert idx.account == _account()
    assert idx.grain == "month"
    # Two grain files written: 2019/08.toon + 2019/09.toon.
    assert (root / "2019" / "08.toon").exists()
    assert (root / "2019" / "09.toon").exists()
    assert summary.months_total == 2


def test_resume_uses_cached_index_toon(tmp_path):
    """``index.toon`` present + ``start=None`` → no discovery RPCs.

    The probe would normally cost ~5+ yearly + ~7+ monthly RPCs. With
    the cache hit we expect zero ``diary_range`` calls beyond the
    actual fetch range.
    """
    root = tmp_path / "bkup"
    write_index_file(
        root / "index.toon",
        IndexDoc(
            account=_account(),
            grain="month",
            discovered_earliest_day=date(2019, 8, 14),
            discovered_at="2026-06-12T20:00:00+00:00",
        ),
    )

    li = FakeLoseIt()
    summary = backup(
        li,
        root=root,
        grain="month",
        start=None,
        end=date(2019, 8, 31),
        today=date(2019, 8, 31),
        sleep_seconds=0.0,
    )

    # Only the fetch RPC fired — no discovery probes.
    assert len(li.diary_range_calls) == 1
    # That call covered August 2019 (the cached earliest day's month).
    assert li.diary_range_calls[0] == (date(2019, 8, 1), date(2019, 8, 31))
    # Summary written.
    assert summary.months_total == 1


def test_partial_grain_file_is_refetched(tmp_path):
    """A grain file pinned to a different account is treated as partial.

    Spec §4.1 says grain files are stateless — anything on disk that
    doesn't match the running account is re-fetched. We mirror that by
    seeding a file pinned to user_id=99, running backup as user_id=12345678,
    and asserting the orchestrator counted it as partial + the file
    on disk now belongs to the new user.
    """
    root = tmp_path / "bkup"
    other_account = AccountRef(user_id="99", user_name="other.user")
    _seed_grain_file(
        root / "2016" / "02.toon",
        grain_kind="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
        account=other_account,
    )

    li = FakeLoseIt()
    reports: list[GrainReport] = []
    summary = backup(
        li,
        root=root,
        grain="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
        sleep_seconds=0.0,
        progress=reports.append,
    )

    # The on-disk file was rewritten under the running account.
    doc = read_grain_file(root / "2016" / "02.toon")
    assert doc.account == _account()
    # And the summary called it partial.
    assert summary.months_partial == 1
    assert summary.months_fetched == 1


# ── Cheap-mode restore tests ────────────────────────────────────────────────


def test_strict_account_refuses_wrong_user(tmp_path):
    """Restore from a grain file pinned to a different user_id is refused.

    Spec §8: ``--strict-account`` is the failsafe. The cheap restore
    must raise rather than re-log entries that don't belong to the
    running user.
    """
    root = tmp_path / "bkup"
    other = AccountRef(user_id="99", user_name="other.user")
    _seed_grain_file(
        root / "2016" / "02.toon",
        grain_kind="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
        account=other,
    )

    li = FakeLoseIt()
    with pytest.raises(ValueError, match="strict_account"):
        restore_backup_cheap(
            li,
            root=root,
            grain="month",
            sleep_seconds=0.0,
        )


def test_cheap_restore_skips_non_empty_grain(tmp_path):
    """Cheap restore: server has data → skip the grain wholesale.

    Two archived grains:
    * 2016/02 — server empty, 1 archived entry → restored (logged).
    * 2024/03 — server has data on day 10 → skipped after day-10 hit.

    Validates spec §7.2's early-exit + spec §7.2's "log all if all
    empty" behavior.
    """
    root = tmp_path / "bkup"
    # 2016/02 grain — one entry to log.
    _seed_grain_file(
        root / "2016" / "02.toon",
        grain_kind="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
        entries=[_grain_entry_for_restore(food_id="a" * 32, day=date(2016, 2, 15))],
    )
    # 2024/03 grain — five entries on disk (will all be skipped because
    # the server has data on day 10).
    _seed_grain_file(
        root / "2024" / "03.toon",
        grain_kind="month",
        start=date(2024, 3, 1),
        end=date(2024, 3, 31),
        entries=[
            _grain_entry_for_restore(food_id="b" * 32, day=date(2024, 3, 5)),
            _grain_entry_for_restore(food_id="c" * 32, day=date(2024, 3, 12)),
        ],
    )

    # Server policy: empty for 2016, non-empty on 2024-03-10.
    def diary_policy(when: date) -> list[FoodLogEntry]:
        if when == date(2024, 3, 10):
            return [_fake_fle()]
        return []

    li = FakeLoseIt(diary_policy=diary_policy)
    reports: list[CheapRestoreGrainReport] = []
    summary = restore_backup_cheap(
        li,
        root=root,
        grain="month",
        sleep_seconds=0.0,
        progress=reports.append,
    )

    # Two grain reports — first is 2016 (restored), second is 2024 (skipped).
    assert isinstance(summary, RestoreSummary)
    assert summary.grains_scanned == 2
    assert summary.grains_skipped == 1
    assert summary.grains_restored == 1
    assert summary.entries_logged == 1

    by_status = {r.status: r for r in reports}
    assert "restore" in by_status and "skip" in by_status
    # 2024 was skipped on day 10 (not day 1, not day 31).
    skipped = by_status["skip"]
    assert skipped.hit_day == date(2024, 3, 10)
    # The orchestrator walked Feb 1..28 = at least 10 days before hit.
    assert skipped.days_scanned == 10
    # 2016 logged the single archived entry.
    restored = by_status["restore"]
    assert restored.entries_logged == 1
    assert restored.days_scanned == 29  # all 29 days of Feb 2016 walked

    # And the actual log_food call fired with the spec §4.4 minimum payload.
    assert len(li.log_calls) == 1
    call = li.log_calls[0]
    assert call["food"] == "a" * 32
    assert call["meal"] == 3
    assert call["servings"] == 0.1
    assert call["when"] == date(2016, 2, 15)


def test_cheap_restore_walks_all_days_in_empty_grain(tmp_path):
    """Spec §7.2: cheap mode reads every day to decide "all empty".

    A "1-3 probe days" shortcut would miss sparse-logging users. The
    test pins this by seeding a grain whose server is empty everywhere
    and asserting we issued exactly one ``diary`` call per day in the
    grain.
    """
    root = tmp_path / "bkup"
    _seed_grain_file(
        root / "2016" / "02.toon",
        grain_kind="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
    )

    li = FakeLoseIt()  # default diary_policy → always empty
    summary = restore_backup_cheap(
        li,
        root=root,
        grain="month",
        sleep_seconds=0.0,
    )

    # 29 day probes — every day of Feb 2016 was walked.
    assert len(li.diary_calls) == 29
    assert li.diary_calls[0] == date(2016, 2, 1)
    assert li.diary_calls[-1] == date(2016, 2, 29)
    # Empty archive → nothing to log.
    assert li.log_calls == []
    assert summary.grains_restored == 1
    assert summary.entries_logged == 0


def test_cheap_restore_dry_run_does_not_log(tmp_path):
    """``dry_run=True`` still reads the server, but issues no log_food."""
    root = tmp_path / "bkup"
    _seed_grain_file(
        root / "2016" / "02.toon",
        grain_kind="month",
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
        entries=[_grain_entry_for_restore(food_id="a" * 32, day=date(2016, 2, 15))],
    )

    li = FakeLoseIt()
    summary = restore_backup_cheap(
        li,
        root=root,
        grain="month",
        sleep_seconds=0.0,
        dry_run=True,
    )

    # Read pass happened.
    assert len(li.diary_calls) == 29
    # But no logs fired.
    assert li.log_calls == []
    # The summary still counts the grain as "would restore".
    assert summary.grains_restored == 1
    assert summary.entries_logged == 0


def test_safe_mode_raises_not_implemented():
    """The ``LoseIt.restore_backup`` default raises until T7 lands.

    This contract makes the migration boundary explicit: callers who
    forget the cheap-mode flag get an error pointing at the flag they
    can pass, not a silent fallback.
    """
    # The method is on :class:`LoseIt`. We don't construct one (that
    # would require a config/token); we call it through ``__func__`` on
    # a fake-shaped object so the NotImplementedError fires before any
    # network setup.
    from lose_it import LoseIt

    fake = FakeLoseIt()
    with pytest.raises(NotImplementedError, match="skip_restore_on_nonempty_grain_time_ranges"):
        LoseIt.restore_backup(fake, root=Path("/tmp/whatever"))  # type: ignore[arg-type]


# ── Helpers used by the discovery test ──────────────────────────────────────


def _fake_fle() -> FoodLogEntry:
    """Minimum-viable FLE so the discovery / restore probes see "an entry"."""
    return FoodLogEntry(
        food_category="C",
        food_name="N",
        food_brand="B",
        food_pk_response=[1] * 16,
        entry_pk_response=[2] * 16,
        entry_day_key="",
        context_day_key="",
        day_num=16832,
        hours_from_gmt=0,
        meal_ordinal=0,
        extra_ordinal=3,
        food_measure_ordinal=27,
        servings=1.0,
        food_identifier_code="",
    )


# ── _enumerate_grains tests (pin spec §2 layout edge cases) ─────────────────


def test_enumerate_month_grains_clips_to_calendar_months():
    """A range that starts/ends mid-month yields whole-month grains.

    Spec §2: grain files are named per calendar month. A backup
    spanning Feb-15..Apr-3 covers three month grains (Feb, Mar, Apr).
    """
    grains = _enumerate_grains(date(2016, 2, 15), date(2016, 4, 3), "month")
    assert len(grains) == 3
    assert grains[0].start == date(2016, 2, 1)
    assert grains[0].end == date(2016, 2, 29)
    assert grains[1].start == date(2016, 3, 1)
    assert grains[2].start == date(2016, 4, 1)


def test_enumerate_day_grains_one_per_day():
    grains = _enumerate_grains(date(2016, 2, 28), date(2016, 3, 2), "day")
    assert [g.start for g in grains] == [
        date(2016, 2, 28),
        date(2016, 2, 29),
        date(2016, 3, 1),
        date(2016, 3, 2),
    ]


def test_grain_file_path_month_layout(tmp_path):
    g = Grain.month(date(2016, 2, 15))
    assert _grain_file_path(tmp_path, g) == tmp_path / "2016" / "02.toon"


def test_grain_file_path_day_layout(tmp_path):
    g = Grain.day(date(2016, 2, 15))
    assert _grain_file_path(tmp_path, g) == tmp_path / "2016" / "02" / "15.toon"


def test_grain_file_path_week_layout(tmp_path):
    # 2016-02-15 is a Monday; ISO week 2016-W07.
    g = Grain.week(date(2016, 2, 15))
    assert _grain_file_path(tmp_path, g) == tmp_path / "2016" / "W07.toon"
