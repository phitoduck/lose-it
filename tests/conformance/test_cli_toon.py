"""CLI-layer conformance tests for ``--output toon``.

Mirrors the JSON-output tests in :mod:`test_cli`. The contract is: every
command that emits a JSON document on stdout under ``-o json`` emits the
same data — round-trippable back through ``toon_format.decode`` — under
``-o toon``, in a more compact, token-efficient form.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import toon_format
from typer.testing import CliRunner

from lose_it_utils.cli import app

SERVICE_URL = "https://www.loseit.com/web/service"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same env shim as :mod:`test_cli` — kept local so this file runs standalone."""
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


def _decode_toon(output: str) -> dict:
    """Parse the CLI's stdout as a TOON document."""
    return toon_format.decode(output)


# ── whoami ──────────────────────────────────────────────────────────────────


def test_whoami_toon_output(env, runner: CliRunner) -> None:
    """``-o toon whoami`` decodes back to the same payload as ``-o json``."""
    json_result = runner.invoke(app, ["-o", "json", "whoami"])
    toon_result = runner.invoke(app, ["-o", "toon", "whoami"])
    assert toon_result.exit_code == 0, toon_result.output
    assert _decode_toon(toon_result.output) == json.loads(json_result.output)


def test_whoami_toon_is_not_json(env, runner: CliRunner) -> None:
    """The TOON document is materially different from the JSON one.

    Guards against a regression where the TOON branch silently falls through
    to ``json.dumps`` (which would still parse cleanly but defeats the
    purpose of the flag).
    """
    result = runner.invoke(app, ["-o", "toon", "whoami"])
    assert result.exit_code == 0
    # TOON does not wrap top-level objects in braces.
    assert not result.output.lstrip().startswith("{")
    # And it does emit the YAML-style ``key: value`` lines.
    assert "user_id:" in result.output


# ── search ──────────────────────────────────────────────────────────────────


def test_search_toon_output(env, runner: CliRunner, httpx_mock) -> None:
    """``-o toon search`` round-trips through ``toon_format.decode``."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    json_result = runner.invoke(app, ["-o", "json", "search", "tortilla"])
    toon_result = runner.invoke(app, ["-o", "toon", "search", "tortilla"])
    assert toon_result.exit_code == 0, toon_result.output
    decoded = _decode_toon(toon_result.output)
    assert decoded == json.loads(json_result.output)
    assert decoded["query"] == "tortilla"
    assert decoded["count"] == len(decoded["results"])


def test_search_toon_is_more_compact_than_json(env, runner: CliRunner, httpx_mock) -> None:
    """The whole point of ``-o toon``: it's noticeably smaller than ``-o json``.

    ``-o toon`` is meant for piping into LLMs, so a regression that bloats
    the output would silently undo the value of the flag. We assert at least
    a 25% character reduction on a representative search payload — well
    below the ~40-60% advertised range, leaving headroom for legitimate
    format tweaks in toon-format upstream.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=(FIXTURES / "search_foods_tortilla.txt").read_text(),
    )
    json_result = runner.invoke(app, ["-o", "json", "search", "tortilla"])
    toon_result = runner.invoke(app, ["-o", "toon", "search", "tortilla"])
    assert json_result.exit_code == 0 and toon_result.exit_code == 0
    j, t = len(json_result.output), len(toon_result.output)
    assert t < j * 0.75, f"TOON ({t} chars) is not <75% of JSON ({j} chars)"


# ── diary ───────────────────────────────────────────────────────────────────


def test_diary_toon_output(env, runner: CliRunner, httpx_mock) -> None:
    """``-o toon diary`` round-trips and carries the same entries as JSON."""
    # 4 responses total (2× init + 2× daily) — one pair per CLI invocation.
    for _ in range(2):
        httpx_mock.add_response(
            url=SERVICE_URL,
            text=(FIXTURES / "get_initialization_data.txt").read_text(),
        )
        httpx_mock.add_response(
            url=SERVICE_URL,
            text=(FIXTURES / "get_daily_details_with_tortilla.txt").read_text(),
        )
    json_result = runner.invoke(app, ["-o", "json", "diary", "--date", "2026-06-08"])
    toon_result = runner.invoke(app, ["-o", "toon", "diary", "--date", "2026-06-08"])
    assert toon_result.exit_code == 0, toon_result.output
    assert _decode_toon(toon_result.output) == json.loads(json_result.output)


# ── log ─────────────────────────────────────────────────────────────────────


def test_log_dry_run_toon_does_not_post_update(env, runner: CliRunner, httpx_mock) -> None:
    """The dry-run guard fires for ``-o toon`` too — no updateFoodLogEntry call."""
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
            "toon",
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
    payload = _decode_toon(result.output)
    assert payload["action"] == "log"
    assert payload["dry_run"] is True
    assert payload["meal"] == "lunch"

    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert not any("updateFoodLogEntry" in b for b in sent_bodies), (
        "dry-run should NOT post updateFoodLogEntry, regardless of --output"
    )


def test_log_missing_food_id_and_query_toon_exits_2(env, runner: CliRunner) -> None:
    """Error payloads render as TOON on validation failures."""
    result = runner.invoke(
        app,
        ["-o", "toon", "log", "--meal", "snacks", "--servings", "1"],
    )
    assert result.exit_code == 2, result.output
    payload = _decode_toon(result.output)
    assert payload["error"] == "missing_food"


# ── delete ──────────────────────────────────────────────────────────────────


def test_delete_dry_run_toon_does_not_post_delete(env, runner: CliRunner, httpx_mock) -> None:
    """The delete dry-run guard also holds under ``-o toon``."""
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
            "toon",
            "delete",
            "--meal",
            "snacks",
            "--pick",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = _decode_toon(result.output)
    assert payload["action"] == "delete"
    assert payload["dry_run"] is True

    sent_bodies = [req.content.decode() for req in httpx_mock.get_requests()]
    assert not any("deleteFoodLogEntry" in b for b in sent_bodies)
