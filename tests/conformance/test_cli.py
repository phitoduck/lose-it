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

from lose_it.cli import app

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
    result = runner.invoke(app, ["whoami", "--output", "json"])
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
    result = runner.invoke(app, ["whoami", "-o", "json"])
    assert result.exit_code == 0
    json.loads(result.output)  # raises if malformed


# ── search ──────────────────────────────────────────────────────────────────


def test_search_json_output(env, runner: CliRunner, httpx_mock) -> None:
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    result = runner.invoke(app, ["search", "tortilla", "-o", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["query"] == "tortilla"
    assert isinstance(payload["results"], list)
    assert payload["count"] == len(payload["results"])
    assert payload["count"] > 0
    for r in payload["results"]:
        # Default output omits the raw byte array — ``food_id`` is the
        # only identifier the CLI itself accepts (--food-id, describe-food).
        assert set(r) == {"name", "brand", "category", "food_id"}
        # ``food_id`` is the 32-char lowercase-hex form of the food's PK.
        assert isinstance(r["food_id"], str)
        assert len(r["food_id"]) == 32
        assert r["food_id"] == r["food_id"].lower()


def test_search_json_output_verbose_includes_pk_bytes(env, runner: CliRunner, httpx_mock) -> None:
    """``-v`` adds the raw 16-int ``pk_bytes`` array next to ``food_id``."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    result = runner.invoke(app, ["search", "tortilla", "-v", "-o", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for r in payload["results"]:
        assert set(r) == {"name", "brand", "category", "food_id", "pk_bytes"}
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
    result = runner.invoke(app, ["diary", "--date", "2026-06-08", "-o", "json"])
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
            "log",
            "tortilla",
            "--meal",
            "lunch",
            "--pick",
            "1",
            "--servings",
            "1",
            "--dry-run",
            "-o",
            "json",
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


def test_log_serving_amount_requires_serving_unit(env, runner: CliRunner) -> None:
    """``--serving-amount N`` alone errors — must be paired with ``--serving-unit``.

    We deliberately don't apply a default unit: a default like 'g' would
    silently misinterpret '--serving-amount 2' for foods natively measured
    in 'each' or 'serving' (e.g. logging 2 g of tortilla when the user
    meant 2 servings). The user must pass the unit explicitly.
    """
    result = runner.invoke(
        app,
        [
            "log",
            "tortilla",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--serving-amount",
            "100",
            # NOTE: no --serving-unit
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "serving_pair_incomplete"


def test_log_grams_rejected_on_non_gram_measured_food(env, runner: CliRunner, httpx_mock) -> None:
    """``--serving-unit g`` errors when the food's native unit isn't grams.

    The captured tortilla fixture has ``food_measure_ordinal=27`` (Serving).
    Cross-class conversions (serving↔grams) aren't supported without per-food
    density data, so the request should bail with ``unit_not_supported``
    and exit code 2.
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
            "log",
            "tortilla",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--serving-amount",
            "100",
            "--serving-unit",
            "g",
            "-o",
            "json",
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
        ["log", "tortilla", "--meal", "lunch", "--pick", "1", "-o", "json"],
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
            "delete",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--dry-run",
            "-o",
            "json",
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
        ["delete", "--meal", "snacks", "--pick", "1", "--yes", "-o", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert any("deleteFoodLogEntry" in b for b in sent_bodies)


# ── log --food-id ────────────────────────────────────────────────────────────


# Any valid 32-char hex string works for the unit-test invocations; the wire
# bytes that come back depend on the mocked response, not the request PK.
_VALID_FOOD_ID = "9eba9129b8494967c8cb3385acf0f614"


def test_log_with_food_id_dry_run(env, runner: CliRunner, httpx_mock) -> None:
    """``log --food-id <hex>`` skips searchFoods, calls getFood + unsaved-entry."""
    # First RPC: getFood — reuse the tortilla unsaved fixture (same FoodIdentifier shape).
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    # Second RPC: getUnsavedFoodLogEntry.
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "log",
            "--food-id",
            _VALID_FOOD_ID,
            "--meal",
            "snacks",
            "--servings",
            "1",
            "--dry-run",
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "log"
    assert payload["dry_run"] is True
    assert payload["meal"] == "snacks"
    # The food_id round-trips into the response (selected.pk_bytes was the
    # caller-supplied PK; pk_to_hex is its lowercase-hex view).
    assert payload["food"]["food_id"] == _VALID_FOOD_ID

    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    # No searchFoods call — went straight via getFood.
    assert not any("searchFoods" in b for b in sent_bodies)
    assert any("getFood" in b and "getUnsavedFoodLogEntry" not in b for b in sent_bodies)
    assert any("getUnsavedFoodLogEntry" in b for b in sent_bodies)
    # And no mutating write in dry-run.
    assert not any("updateFoodLogEntry" in b for b in sent_bodies)


def test_log_food_id_text_output_includes_id_prefix(env, runner: CliRunner, httpx_mock) -> None:
    """The text success line includes a ``(id <prefix>...)`` tag."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    result = runner.invoke(
        app,
        [
            "log",
            "--food-id",
            _VALID_FOOD_ID,
            "--meal",
            "snacks",
            "--servings",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # Text output includes the first 4 hex chars of the ID.
    assert "(id 9eba" in result.output


def test_log_food_id_invalid_hex_exits_2(env, runner: CliRunner) -> None:
    """Non-hex --food-id values are rejected with exit code 2."""
    result = runner.invoke(
        app,
        [
            "log",
            "--food-id",
            "NOTHEXNOTHEXNOTHEXNOTHEXNOTHEX!!",
            "--meal",
            "snacks",
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "invalid_food_id"


def test_log_food_id_wrong_length_exits_2(env, runner: CliRunner) -> None:
    """Hex strings of the wrong length are rejected with exit code 2."""
    result = runner.invoke(
        app,
        ["log", "--food-id", "9eba", "--meal", "snacks", "-o", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "invalid_food_id"


def test_log_food_id_mutually_exclusive_with_query(env, runner: CliRunner) -> None:
    """Passing both --food-id and a positional query exits 2."""
    result = runner.invoke(
        app,
        ["log", "tortilla", "--food-id", _VALID_FOOD_ID, "--meal", "snacks", "-o", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "mutually_exclusive"


def test_log_food_id_mutually_exclusive_with_pick(env, runner: CliRunner) -> None:
    """Passing both --food-id and --pick exits 2."""
    result = runner.invoke(
        app,
        [
            "log",
            "--food-id",
            _VALID_FOOD_ID,
            "--pick",
            "1",
            "--meal",
            "snacks",
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "mutually_exclusive"


def test_log_missing_food_id_and_query_exits_2(env, runner: CliRunner) -> None:
    """Passing neither --food-id nor a positional query exits 2."""
    result = runner.invoke(
        app,
        ["log", "--meal", "snacks", "--servings", "1", "-o", "json"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "missing_food"


def test_log_food_id_real_run_posts_update(env, runner: CliRunner, httpx_mock) -> None:
    """Without --dry-run, ``--food-id`` still drives the mutating updateFoodLogEntry."""
    # getFood
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    # getUnsavedFoodLogEntry
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_unsaved_tortilla.txt").read_text(),
    )
    # getInitializationData (day_key lookup)
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "get_initialization_data.txt").read_text(),
    )
    # updateFoodLogEntry
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "update_food_log_entry_success.txt").read_text(),
    )
    result = runner.invoke(
        app,
        ["log", "--food-id", _VALID_FOOD_ID, "--meal", "snacks", "-o", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert any("updateFoodLogEntry" in b for b in sent_bodies)
    assert not any("searchFoods" in b for b in sent_bodies)


def test_search_text_output_has_food_id_column(env, runner: CliRunner, httpx_mock) -> None:
    """The default text search table includes a Food ID column."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    result = runner.invoke(app, ["search", "tortilla"])
    assert result.exit_code == 0, result.output
    assert "Food ID" in result.output
