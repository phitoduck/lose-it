"""Live-API ``--dry-run`` test for the CLI.

Confirms that:

1. Running ``log <food> --dry-run`` against the real Lose It! API does NOT
   add anything to the diary (read-only lookups only).
2. Running ``delete --dry-run`` against the real API does NOT remove
   anything from the diary, even when there's a real entry to target.

Marked ``requires_auth`` — skipped by default; pass ``pytest --run-auth`` to opt in.
Requires the same env vars + token as the rest of the requires_auth suite.
"""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from lose_it import Client
from lose_it.cli import app
from lose_it.core import daily

pytestmark = pytest.mark.requires_auth


def _diary_size(when: date) -> int:
    with Client.from_env() as client:
        return len(daily.get_daily_details(client.http, when))


def test_log_dry_run_does_not_mutate_real_diary() -> None:
    """`log … --dry-run` against the live API leaves the diary count unchanged."""
    when = date.today()
    before = _diary_size(when)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "log",
            "x-treme carb balance tortilla",
            "--meal",
            "snacks",
            "--pick",
            "2",
            "--servings",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output or "would log" in result.output.lower()

    after = _diary_size(when)
    assert after == before, (
        f"diary size should be unchanged after --dry-run; before={before}, after={after}"
    )


def test_delete_dry_run_does_not_mutate_real_diary() -> None:
    """`delete … --dry-run` against the live API leaves the diary count unchanged.

    Requires at least one snacks entry to target — we log one first (real call),
    capture the count, run the dry-run delete, then clean up the entry.
    """
    when = date.today()
    runner = CliRunner()

    # Real log so there's something to "delete" — we'll clean up after.
    log_result = runner.invoke(
        app,
        [
            "log",
            "x-treme carb balance tortilla",
            "--meal",
            "snacks",
            "--pick",
            "2",
            "--servings",
            "1",
        ],
    )
    assert log_result.exit_code == 0, log_result.output

    after_log = _diary_size(when)

    # Dry-run delete the freshly-added entry. Diary count must NOT change.
    dry = runner.invoke(
        app,
        ["delete", "--meal", "snacks", "--pick", "1", "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert "DRY RUN" in dry.output or "would delete" in dry.output.lower()
    assert _diary_size(when) == after_log, "dry-run delete should not change diary size"

    # Real delete to clean up after ourselves.
    cleanup = runner.invoke(
        app,
        ["delete", "--meal", "snacks", "--pick", "1", "--yes"],
    )
    assert cleanup.exit_code == 0, cleanup.output
    assert _diary_size(when) == after_log - 1, "cleanup delete should remove one entry"
