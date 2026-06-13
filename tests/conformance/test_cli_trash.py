"""CLI tests for the trash-aware ``loseit delete`` and ``loseit restore-trash``.

These tests exercise the full Typer pipeline but stub the wire layer so
they're hermetic — no real Lose It! account needed.

The invariants:

- ``loseit delete --no-trash`` (without --i-know-this-is-unrecoverable)
  exits 2 with the exact BDD-pinned stderr; no wire delete fires.
- ``loseit delete --no-trash --i-know-this-is-unrecoverable`` deletes
  without writing trash; stdout includes the "<none>" sink-pointer line.
- A successful default delete writes one JSONL line to the trash file
  and exits 0; stdout includes the "trash sink: ..." line.
- ``loseit restore-trash`` (default) consumes the last line of trash.jsonl
  and re-logs the entry via :meth:`LoseIt.log_food`.
- ``loseit restore-trash --line N --keep`` leaves the file untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lose_it.cli import app

SERVICE_URL = "https://www.loseit.com/web/service"
FIXTURES = Path(__file__).parent / "fixtures"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Mirror :func:`tests.conformance.test_cli.env` and return ``HOME``.

    Returning ``HOME`` lets each test poke at the default trash file
    location without rebuilding the path inline.
    """
    monkeypatch.setenv("LOSEIT_USER_ID", "12345678")
    monkeypatch.setenv("LOSEIT_USER_NAME", "test.user")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "-6")
    monkeypatch.setenv("LOSEIT_POLICY_HASH", "8F87EC8969F17AE77B6283D3A83F6D4C")
    monkeypatch.setenv("LOSEIT_STRONG_NAME", "351AE5DC0CA36AD3BA9C7CBA7B0E07B8")
    monkeypatch.setenv("LOSEIT_TOKEN", "fake-jwt-token")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def runner() -> CliRunner:
    # mix_stderr=False so we can pin the exact stderr blob from the BDDs.
    # CliRunner in newer Typer/Click versions accepts the kwarg; if not,
    # we still get a combined stream and assert against ``result.output``.
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # pragma: no cover - depends on click version
        return CliRunner()


# ── `loseit delete --no-trash` gating ────────────────────────────────────────


def test_cli_delete_no_trash_without_ack_exits_2(env: Path, runner: CliRunner, httpx_mock) -> None:
    """--no-trash alone exits 2 with the exact stderr blob from BDDs."""
    # The gate fires before any HTTP is needed — no mocks required.
    result = runner.invoke(
        app,
        ["delete", "--meal", "snacks", "--pick", "1", "--yes", "--no-trash"],
    )
    assert result.exit_code == 2, result.output
    stderr = result.stderr if hasattr(result, "stderr") else result.output
    assert "error: refusing to delete without a trash sink" in stderr
    assert "hint:  pass --i-know-this-is-unrecoverable to override" in stderr
    assert "(this discards any chance of recovering the entry)" in stderr
    # No wire calls at all.
    sent = [req.content.decode() for req in httpx_mock.get_requests()]
    assert sent == []


def test_cli_delete_no_trash_with_ack_deletes_without_trash(
    env: Path, runner: CliRunner, httpx_mock
) -> None:
    """--no-trash + --i-know-this-is-unrecoverable deletes; no trash file."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_daily_details_with_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "delete_food_log_entry_success.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "delete",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--yes",
            "--no-trash",
            "--i-know-this-is-unrecoverable",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "trash sink: <none — caller acknowledged --no-trash>" in result.output
    assert "✅ Deleted" in result.output
    # Wire delete went out.
    sent = [req.content.decode() for req in httpx_mock.get_requests()]
    assert any("deleteFoodLogEntry" in b for b in sent)
    # No trash file was created.
    default_trash = env / ".config" / "loseit" / "trash.jsonl"
    assert not default_trash.exists()


# ── `loseit delete` default path writes trash ────────────────────────────────


def test_cli_delete_default_writes_trash_jsonl(env: Path, runner: CliRunner, httpx_mock) -> None:
    """Default delete writes one JSONL line to ~/.config/loseit/trash.jsonl."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_daily_details_with_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "delete_food_log_entry_success.txt").read_text(),
    )
    result = runner.invoke(
        app,
        ["delete", "--meal", "snacks", "--pick", "1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    trash = env / ".config" / "loseit" / "trash.jsonl"
    assert trash.exists()
    assert trash.read_text().count("\n") == 1
    obj = json.loads(trash.read_text().strip())
    assert "entry" in obj
    assert "stashed_at" in obj
    # stdout mentions the sink pointer and the restore hint.
    assert "trash sink:" in result.output
    assert "loseit restore-trash" in result.output
    assert "✅ Deleted" in result.output


def test_cli_delete_custom_trash_file_writes_there(
    env: Path, runner: CliRunner, httpx_mock, tmp_path: Path
) -> None:
    """--trash-file overrides the default path."""
    custom = tmp_path / "custom-trash.jsonl"
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_daily_details_with_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "delete_food_log_entry_success.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "delete",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--yes",
            "--trash-file",
            str(custom),
        ],
    )
    assert result.exit_code == 0, result.output
    assert custom.exists()
    assert custom.read_text().count("\n") == 1
    # Default path was NOT used.
    default_trash = env / ".config" / "loseit" / "trash.jsonl"
    assert not default_trash.exists()


def test_cli_delete_readonly_trash_aborts_wire_delete(
    env: Path, runner: CliRunner, httpx_mock, tmp_path: Path
) -> None:
    """A trash write failure aborts the wire delete with exit code 2."""
    # Create a directory where the trash file ought to be — open() of a
    # path under a non-existent parent that's a *file* triggers an
    # IsADirectoryError when we try to open(target, 'a').
    bad_dir = tmp_path / "trash-as-dir"
    bad_dir.mkdir()
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_daily_details_with_tortilla.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "delete",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--yes",
            "--trash-file",
            str(bad_dir),  # opening a directory as 'a' raises
        ],
    )
    assert result.exit_code == 2, result.output
    stderr = result.stderr if hasattr(result, "stderr") else result.output
    assert "trash sink: cannot write" in stderr
    assert "the wire delete was NOT sent" in stderr
    # The wire delete did NOT fire.
    sent = [req.content.decode() for req in httpx_mock.get_requests()]
    assert not any("deleteFoodLogEntry" in b for b in sent)


# ── `loseit restore-trash` ──────────────────────────────────────────────────


def _seed_trash(path: Path, *, food_id: str, food_name: str, servings: float, date: str) -> None:
    """Drop a single JSONL record at ``path``."""
    record = {
        "stashed_at": "2026-06-12T20:00:00+00:00",
        "user_name": "test.user",
        "entry": {
            "food_id": food_id,
            "food_name": food_name,
            "meal": "snacks",
            "date": date,
            "servings": servings,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _seed_trash_lines(path: Path, n: int) -> None:
    """Write ``n`` JSONL records (each a distinct food_id)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(n):
        record = {
            "stashed_at": "2026-06-12T20:00:00+00:00",
            "user_name": "test.user",
            "entry": {
                "food_id": f"{i:032x}",
                "food_name": f"line-{i}",
                "meal": "snacks",
                "date": "2026-06-12",
                "servings": float(i + 1),
            },
        }
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cli_restore_trash_dry_run_does_not_consume_file(
    env: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run prints the plan; the file is unchanged."""
    trash = env / ".config" / "loseit" / "trash.jsonl"
    _seed_trash(trash, food_id="a" * 32, food_name="Stub", servings=0.1, date="2016-02-15")

    # Belt-and-braces: forbid any log_food call.
    from lose_it.client import LoseIt as _LoseIt

    def _refuse(*args, **kwargs):
        raise AssertionError("log_food should not be called for --dry-run")

    monkeypatch.setattr(_LoseIt, "log_food", _refuse)

    result = runner.invoke(app, ["restore-trash", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would restore trash#1" in result.output
    assert "no log RPC sent (dry run)." in result.output
    # File still has its one line.
    assert trash.read_text().count("\n") == 1


def test_cli_restore_trash_consumes_by_default(
    env: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 line in trash.jsonl -> 0 lines after `loseit restore-trash`."""
    trash = env / ".config" / "loseit" / "trash.jsonl"
    _seed_trash(trash, food_id="a" * 32, food_name="Wrap", servings=0.1, date="2016-02-15")

    from lose_it.client import LoseIt as _LoseIt
    from lose_it.models import FoodSearchResult, LoggedFood

    def _fake_log_food(self, food, meal, servings, **kwargs):
        return LoggedFood(
            food=FoodSearchResult(
                name="Wrap",
                brand="",
                category="",
                pk_bytes=[0] * 16,
            ),
            meal_ordinal=3,
            meal_name="snacks",
            when="2016-02-15",
            canonical_servings=servings,
            portion_amount=servings,
            portion_unit="serving",
            calories=70.0,
            dry_run=False,
        )

    monkeypatch.setattr(_LoseIt, "log_food", _fake_log_food)

    result = runner.invoke(app, ["restore-trash"])
    assert result.exit_code == 0, result.output
    assert "restoring trash#1 (last line)" in result.output
    assert "logged successfully" in result.output
    assert "trash#1 consumed." in result.output
    # The file is now empty (0 lines).
    assert trash.read_text() == ""


def test_cli_restore_trash_keep_preserves_file(
    env: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 lines + --keep --line 2 -> 3 lines, line 2 unchanged."""
    trash = env / ".config" / "loseit" / "trash.jsonl"
    _seed_trash_lines(trash, 3)
    before = trash.read_text()
    assert before.count("\n") == 3

    from lose_it.client import LoseIt as _LoseIt
    from lose_it.models import FoodSearchResult, LoggedFood

    def _fake_log_food(self, food, meal, servings, **kwargs):
        return LoggedFood(
            food=FoodSearchResult(
                name="x",
                brand="",
                category="",
                pk_bytes=[0] * 16,
            ),
            meal_ordinal=3,
            meal_name="snacks",
            when="2026-06-12",
            canonical_servings=servings,
            portion_amount=servings,
            portion_unit="serving",
            calories=0.0,
            dry_run=False,
        )

    monkeypatch.setattr(_LoseIt, "log_food", _fake_log_food)

    result = runner.invoke(app, ["restore-trash", "--line", "2", "--keep"])
    assert result.exit_code == 0, result.output
    assert "restoring trash#2 (--keep, line will remain after restore)" in result.output
    assert "trash#2 retained." in result.output
    # All 3 lines still present and byte-identical.
    after = trash.read_text()
    assert after == before
    assert after.count("\n") == 3
