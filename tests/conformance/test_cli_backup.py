"""CLI-layer conformance tests for ``loseit backup`` / ``loseit restore-backup``.

Hermetic — every test stubs ``LoseIt`` at the import boundary in
:mod:`lose_it.cli` via :func:`monkeypatch.setattr` on ``_open_loseit``.
We never hit the HTTP layer.

Spec coverage (per ``docs/backup-spec.md``):

* §3.1 — ``loseit backup`` output: per-grain rows, summary block,
  ``--dry-run`` plan, ``--quiet-skips`` skip collapsing.
* §3.2 — ``loseit restore-backup`` output: account/mode header,
  per-grain rows in cheap + safe modes.
* §7.1 / §7.2 — the two restore modes.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lose_it import cli as cli_module
from lose_it.backup import (
    AccountRef,
    GrainBounds,
    GrainDoc,
    GrainEntry,
    write_grain_file,
)
from lose_it.cli import app
from lose_it.models import FoodLogEntry

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _account() -> AccountRef:
    return AccountRef(user_id="12345678", user_name="test.user")


def _grain_entry(
    *,
    food_id: str,
    day: date,
    meal_ordinal: int = 3,
    servings: float = 0.1,
    modified_at: str = "2016-02-15T12:00:00+00:00",
) -> GrainEntry:
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


class _FakeConfig:
    def __init__(self, *, user_id: str = "12345678", user_name: str = "test.user") -> None:
        self.user_id = user_id
        self.user_name = user_name


class StubLoseIt:
    """Stub LoseIt: enough surface for ``backup`` + ``restore-backup`` CLI.

    Records the calls so tests can assert on RPC count + args, and lets
    individual tests pin per-method behavior via ``diary_policy`` /
    ``diary_range_policy``.
    """

    def __init__(
        self,
        *,
        diary_policy=None,
        diary_range_policy=None,
    ) -> None:
        self.config = _FakeConfig()
        self.diary_calls: list[date] = []
        self.diary_range_calls: list[tuple[date, date]] = []
        self.log_calls: list[dict[str, Any]] = []
        self._diary_policy = diary_policy
        self._diary_range_policy = diary_range_policy

    def __enter__(self) -> StubLoseIt:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def close(self) -> None:  # pragma: no cover - lifecycle parity
        pass

    # ── SDK surface (called by the orchestrator) ─────────────────────────

    def diary(self, when: date) -> list[FoodLogEntry]:
        self.diary_calls.append(when)
        if self._diary_policy is None:
            return []
        return self._diary_policy(when)

    def diary_range(self, start: date, end: date) -> dict[date, list[FoodLogEntry]]:
        self.diary_range_calls.append((start, end))
        if self._diary_range_policy is not None:
            return self._diary_range_policy(start, end)
        out: dict[date, list[FoodLogEntry]] = {}
        from datetime import timedelta as _td

        cur = start
        while cur <= end:
            out[cur] = []
            cur = cur + _td(days=1)
        return out

    def describe_food(self, food_id: str):  # not exercised in these tests
        class _D:
            name = "N"
            brand = "B"
            category = "C"

            class primary_serving:
                @staticmethod
                def to_dict() -> dict[str, Any]:
                    return {"ordinal": 27, "unit": "serving"}

            class cross_class_conversion:
                @staticmethod
                def to_dict() -> dict[str, Any]:
                    return {"per_serving_g": None, "per_serving_ml": None}

            nutrients_per_serving: dict[str, float] = {}  # noqa: RUF012
            raw_nutrients_by_ord: dict[str, float] = {}  # noqa: RUF012

        return _D

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
        return None

    # The high-level methods on LoseIt that the CLI calls — delegate to
    # the orchestrator's free functions so the same code paths are
    # exercised end-to-end.

    def backup(self, **kwargs: Any) -> Any:
        from lose_it.backup._orchestrator import backup as _backup

        return _backup(self, **kwargs)

    def restore_backup(self, **kwargs: Any) -> Any:
        from lose_it.backup._orchestrator import (
            restore_backup_cheap as _cheap,
        )
        from lose_it.backup._orchestrator import (
            restore_backup_safe as _safe,
        )

        if kwargs.pop("skip_restore_on_nonempty_grain_time_ranges", False):
            kwargs.pop("upsert_window", None)
            return _cheap(self, **kwargs)
        return _safe(self, **kwargs)


@pytest.fixture
def patch_loseit(monkeypatch: pytest.MonkeyPatch):
    """Install a fresh :class:`StubLoseIt` for the duration of one test.

    Returns the stub so the test can assert on call records / pin
    behavior via ``stub._diary_policy``, etc.
    """
    stub_holder: dict[str, StubLoseIt] = {}

    def _factory(**policies: Any) -> StubLoseIt:
        stub = StubLoseIt(**policies)
        stub_holder["stub"] = stub
        # Patch the cli module's open helper; sleep_seconds=0.0 also lives
        # at the CLI default of 1.0 — tests must pass --sleep-seconds 0 to
        # keep tests fast.
        monkeypatch.setattr(cli_module, "_open_loseit", lambda ctx=None: stub)
        return stub

    return _factory


# ── Tests: backup ───────────────────────────────────────────────────────────


def test_cli_backup_dry_run_creates_no_files(tmp_path, runner: CliRunner, patch_loseit):
    """``loseit backup --dry-run --root /tmp/bkup`` creates no files.

    Pins spec §3.1 ("--dry-run prints the plan and exits") + the BDD
    scenario in impl-plan §6 T8 ("dry-run writes zero files").
    """
    stub = patch_loseit()
    root = tmp_path / "bkup"

    result = runner.invoke(
        app,
        [
            "backup",
            "--root",
            str(root),
            "--start",
            "2016-02-01",
            "--end",
            "2016-02-29",
            "--sleep-seconds",
            "0",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    # No grain file, no foods.toon, no index.toon — dry run is read-only.
    assert not (root / "2016" / "02.toon").exists()
    assert not (root / "foods.toon").exists()
    # No fetch RPCs either.
    assert stub.diary_range_calls == []
    assert stub.diary_calls == []
    # The text output ends with "no RPCs sent." per spec §3.1.
    assert "no RPCs sent." in result.output


def test_cli_backup_writes_grain_file_for_safe_range(tmp_path, runner: CliRunner, patch_loseit):
    """Hermetic: stub diary_range; backup writes ``tmp/2016/02.toon``.

    Mirrors the spec §3.1 first-run BDD: ``loseit backup --root tmp
    --start 2016-02-01 --end 2016-02-29`` should produce
    ``tmp/2016/02.toon`` carrying the expected top-level keys.
    """
    stub = patch_loseit()
    root = tmp_path / "bkup"

    result = runner.invoke(
        app,
        [
            "backup",
            "--root",
            str(root),
            "--start",
            "2016-02-01",
            "--end",
            "2016-02-29",
            "--sleep-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    grain_path = root / "2016" / "02.toon"
    assert grain_path.exists()

    # Re-read the file and confirm the expected top-level keys + bounds.
    from lose_it.backup import read_grain_file

    doc = read_grain_file(grain_path)
    assert doc.grain.kind == "month"
    assert doc.grain.start == date(2016, 2, 1)
    assert doc.grain.end == date(2016, 2, 29)
    assert doc.account == _account()
    # Empty month — no entries.
    assert doc.entries == []

    # foods.toon also created (bootstrap).
    assert (root / "foods.toon").exists()
    # Stub got exactly one diary_range call covering Feb 2016.
    assert stub.diary_range_calls == [(date(2016, 2, 1), date(2016, 2, 29))]
    # Stdout includes the per-grain fetch line and the summary block.
    assert "fetch     2016/02.toon" in result.output
    assert "summary" in result.output


def test_cli_backup_quiet_skips_collapses(tmp_path, runner: CliRunner, patch_loseit):
    """With multiple complete grain files, --quiet-skips produces ONE
    skip-range line, not one-per-grain.

    Pins the spec §3.1 ``--quiet-skips`` example.
    """
    stub = patch_loseit()
    root = tmp_path / "bkup"

    # Seed 5 contiguous complete monthly grains so the skip-run collapses.
    for month_start, month_end in [
        (date(2019, 8, 1), date(2019, 8, 31)),
        (date(2019, 9, 1), date(2019, 9, 30)),
        (date(2019, 10, 1), date(2019, 10, 31)),
        (date(2019, 11, 1), date(2019, 11, 30)),
        (date(2019, 12, 1), date(2019, 12, 31)),
    ]:
        _seed_grain_file(
            root / f"{month_start.year:04d}" / f"{month_start.month:02d}.toon",
            entries=[],
            grain_kind="month",
            start=month_start,
            end=month_end,
        )

    result = runner.invoke(
        app,
        [
            "backup",
            "--root",
            str(root),
            "--start",
            "2019-08-01",
            "--end",
            "2019-12-31",
            "--sleep-seconds",
            "0",
            "--quiet-skips",
        ],
    )

    assert result.exit_code == 0, result.output
    # The collapsed range line appears exactly once and mentions both ends.
    skip_lines = [ln for ln in result.output.splitlines() if ln.startswith("skip")]
    assert len(skip_lines) == 1, result.output
    assert "2019/08.toon .. 2019/12.toon" in skip_lines[0]
    # And we did not emit one line per grain.
    assert "skip      2019/09.toon" not in result.output
    assert "skip      2019/10.toon" not in result.output
    # No fetch RPCs either — every grain was on disk.
    assert stub.diary_range_calls == []


# ── Tests: restore-backup ───────────────────────────────────────────────────


def test_cli_restore_backup_cheap_dry_run(tmp_path, runner: CliRunner, patch_loseit):
    """Hand-crafted archive + stub server. Dry-run cheap mode reports
    skip/restore decisions without firing log RPCs.
    """
    root = tmp_path / "bkup"
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[_grain_entry(food_id="a" * 32, day=date(2016, 2, 15))],
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
    )
    _seed_grain_file(
        root / "2024" / "03.toon",
        entries=[_grain_entry(food_id="b" * 32, day=date(2024, 3, 5))],
        start=date(2024, 3, 1),
        end=date(2024, 3, 31),
    )

    # Server policy: empty for 2016, non-empty on 2024-03-10.
    def diary_policy(when: date) -> list[FoodLogEntry]:
        if when == date(2024, 3, 10):
            return [
                FoodLogEntry(
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
            ]
        return []

    stub = patch_loseit(diary_policy=diary_policy)

    result = runner.invoke(
        app,
        [
            "restore-backup",
            "--root",
            str(root),
            "--skip-restore-on-nonempty-grain-time-ranges",
            "--sleep-seconds",
            "0",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "mode:                 simple" in result.output
    # The 2024/03 grain skipped because day 10 hit; the 2016/02 grain
    # would have logged 1 entry (dry-run, so no actual log calls).
    assert "skip      2024/03.toon" in result.output
    assert "restore   2016/02.toon" in result.output
    # Dry-run → no log_food calls.
    assert stub.log_calls == []


def test_cli_restore_backup_safe_default_mode(tmp_path, runner: CliRunner, patch_loseit):
    """Hand-crafted archive + empty server. Safe-mode default logs every
    archive entry on every day-with-entries.
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
        start=date(2016, 2, 1),
        end=date(2016, 2, 29),
    )

    stub = patch_loseit()  # default diary_policy returns empty

    result = runner.invoke(
        app,
        [
            "restore-backup",
            "--root",
            str(root),
            "--sleep-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "mode:                 safe" in result.output
    # Both archive entries were logged.
    assert len(stub.log_calls) == 2
    assert {c["food"] for c in stub.log_calls} == {food_a, food_b}
    assert all(c["meal"] == 3 for c in stub.log_calls)
    assert all(c["when"] == date(2016, 2, 15) for c in stub.log_calls)
    # Summary block lists the entries logged.
    assert "entries logged:       2" in result.output


def test_cli_restore_backup_json_output(tmp_path, runner: CliRunner, patch_loseit):
    """``-o json`` emits a structured envelope rather than the text block."""
    root = tmp_path / "bkup"
    _seed_grain_file(
        root / "2016" / "02.toon",
        entries=[_grain_entry(food_id="a" * 32, day=date(2016, 2, 15))],
    )

    patch_loseit()
    result = runner.invoke(
        app,
        [
            "-o",
            "json",
            "restore-backup",
            "--root",
            str(root),
            "--sleep-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "restore_backup"
    assert payload["mode"] == "safe"
    assert payload["grains_scanned"] == 1
    assert payload["entries_logged"] == 1


def test_cli_backup_grain_validation(tmp_path, runner: CliRunner, patch_loseit):
    """``--grain year`` is rejected before any RPC is issued (spec §2)."""
    patch_loseit()
    result = runner.invoke(
        app,
        [
            "backup",
            "--root",
            str(tmp_path / "bkup"),
            "--grain",
            "year",
            "--sleep-seconds",
            "0",
        ],
    )
    assert result.exit_code != 0
    # ``typer.BadParameter`` rewrites the error onto stderr.
    combined = result.output + (result.stderr if result.stderr else "")
    assert "day|week|month" in combined or "year" in combined
