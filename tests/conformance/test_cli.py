"""CLI-layer conformance tests for ``--output`` and ``--dry-run``.

Uses Typer's :class:`CliRunner` to invoke the actual command pipeline,
``pytest-httpx`` to mock the GWT-RPC backend, and inspects the on-the-wire
requests that would have been emitted to confirm the mutating endpoints
are NOT called during dry-runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lose_it_utils.cli import app

SERVICE_URL = "https://www.loseit.com/web/service"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Set LOSEIT_* env vars + write a placeholder token file for the duration of a test."""
    monkeypatch.setenv("LOSEIT_USER_ID", "12345678")
    monkeypatch.setenv("LOSEIT_USER_NAME", "test.user")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "-6")
    monkeypatch.setenv("LOSEIT_POLICY_HASH", "8F87EC8969F17AE77B6283D3A83F6D4C")
    monkeypatch.setenv("LOSEIT_STRONG_NAME", "351AE5DC0CA36AD3BA9C7CBA7B0E07B8")
    token_file = tmp_path / "token"
    token_file.write_text("fake-jwt-token")
    monkeypatch.setenv("LOSEIT_TOKEN", "fake-jwt-token")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── whoami ──────────────────────────────────────────────────────────────────


def test_whoami_text_default(env, runner: CliRunner) -> None:
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0, result.output
    assert "user_id" in result.output
    assert "12345678" in result.output
    # Plain text contains the field labels, no braces.
    assert "{" not in result.output


def test_whoami_json_output(env, runner: CliRunner) -> None:
    result = runner.invoke(app, ["--output", "json", "whoami"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "user_id": "12345678",
        "user_name": "test.user",
        "hours_from_gmt": -6,
        "policy_hash": "8F87EC8969F17AE77B6283D3A83F6D4C",
        "strong_name": "351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    }


def test_whoami_short_o_alias(env, runner: CliRunner) -> None:
    """``-o`` is an alias for ``--output``."""
    result = runner.invoke(app, ["-o", "json", "whoami"])
    assert result.exit_code == 0
    json.loads(result.output)  # raises if malformed


# ── search ──────────────────────────────────────────────────────────────────


def test_search_json_output(env, runner: CliRunner, httpx_mock) -> None:
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    result = runner.invoke(app, ["-o", "json", "search", "tortilla"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["query"] == "tortilla"
    assert isinstance(payload["results"], list)
    assert payload["count"] == len(payload["results"])
    assert payload["count"] > 0
    for r in payload["results"]:
        assert set(r) == {"name", "brand", "category", "pk_bytes"}
        assert len(r["pk_bytes"]) == 16


# ── diary ───────────────────────────────────────────────────────────────────


def test_diary_json_output(env, runner: CliRunner, httpx_mock) -> None:
    # 2 responses: getInitializationData (for day_key) + daily details.
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_daily_details_with_tortilla.txt").read_text(),
    )
    result = runner.invoke(app, ["-o", "json", "diary", "--date", "2026-06-08"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["date"] == "2026-06-08"
    assert payload["count"] == len(payload["entries"])
    assert payload["count"] > 0
    for e in payload["entries"]:
        assert "food_name" in e and "ortilla" in e["food_name"]
        assert e["meal"] in {"breakfast", "lunch", "dinner", "snacks"}
        assert len(e["entry_pk"]) == 16
        assert len(e["food_pk"]) == 16


# ── log --dry-run ───────────────────────────────────────────────────────────


def test_log_dry_run_does_not_post_update(env, runner: CliRunner, httpx_mock) -> None:
    """--dry-run runs search + get_unsaved but skips updateFoodLogEntry."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "-o",
            "json",
            "log",
            "tortilla",
            "--meal",
            "lunch",
            "--pick",
            "1",
            "--servings",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "log"
    assert payload["dry_run"] is True
    assert payload["meal"] == "lunch"
    assert payload["servings"] == 1.0
    assert "food" in payload and "name" in payload["food"]

    # Critical: no updateFoodLogEntry RPC was made.
    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert not any("updateFoodLogEntry" in b for b in sent_bodies), (
        "dry-run should NOT post updateFoodLogEntry"
    )
    # But it DID do the read-only lookups.
    assert any("searchFoods" in b for b in sent_bodies)
    assert any("getUnsavedFoodLogEntry" in b for b in sent_bodies)


def test_log_grams_rejected_on_non_gram_measured_food(env, runner: CliRunner, httpx_mock) -> None:
    """--grams errors when the picked food's measure unit isn't grams.

    The captured tortilla fixture has ``food_measure_ordinal=27`` (Serving),
    so trying to use --grams on it should bail out with a helpful message
    and exit code 2 (config-style failure).

    After the serving-unit refactor, ``--grams N`` is rewritten internally
    to ``--serving-amount N --serving-unit g``, so the rejection error
    now flows through the generic ``unit_not_supported`` code path rather
    than the legacy ``not_gram_measured`` one.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "-o",
            "json",
            "log",
            "tortilla",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--grams",
            "100",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "unit_not_supported"
    assert payload["native_unit"] == "serving"
    assert payload["requested_unit"] == "g"

    # And critically: no updateFoodLogEntry call was made.
    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert not any("updateFoodLogEntry" in b for b in sent_bodies)


def test_log_real_run_does_post_update(env, runner: CliRunner, httpx_mock) -> None:
    """Without --dry-run, the mutating updateFoodLogEntry call IS made."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "update_food_log_entry_success.txt").read_text(),
    )
    result = runner.invoke(
        app,
        ["-o", "json", "log", "tortilla", "--meal", "lunch", "--pick", "1"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert any("updateFoodLogEntry" in b for b in sent_bodies)


# ── delete --dry-run ────────────────────────────────────────────────────────


def test_delete_dry_run_does_not_post_delete(env, runner: CliRunner, httpx_mock) -> None:
    """--dry-run lists what would be deleted, sends no deleteFoodLogEntry."""
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
            "-o",
            "json",
            "delete",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "delete"
    assert payload["dry_run"] is True
    assert payload["meal"] == "snacks"
    assert "target" in payload and "ortilla" in payload["target"]["food_name"]

    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert not any("deleteFoodLogEntry" in b for b in sent_bodies), (
        "dry-run should NOT post deleteFoodLogEntry"
    )
    # Still does the read-only getDailyDetails lookup.
    assert any("getDailyDetailsIncludingPendingForDate" in b for b in sent_bodies)


def test_delete_real_run_does_post_delete(env, runner: CliRunner, httpx_mock) -> None:
    """Without --dry-run + with --yes, the mutating deleteFoodLogEntry call IS made."""
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
        ["-o", "json", "delete", "--meal", "snacks", "--pick", "1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert any("deleteFoodLogEntry" in b for b in sent_bodies)
