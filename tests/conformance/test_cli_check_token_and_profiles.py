"""CLI tests for the pre-login probes: ``check-token`` and ``list-profiles``.

These commands let an agent decide *whether* to ask the user about their
browser profile at all — they're meant to be cheap and side-effect-free
(no Keychain prompt, no network). The tests pin both behaviours:

- ``check-token`` reports valid / expired / missing without touching the
  browser cookie store.
- ``list-profiles`` enumerates the user's browser profiles from disk
  alone (Local State JSON + directory names) and never imports
  ``browser_cookie3``.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lose_it.cli import app
from lose_it.core import auth as auth_module


def _make_jwt(payload: dict[str, Any]) -> str:
    header = {"alg": "ES384", "typ": "JWT", "kid": "TESTKID"}

    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64(header)}.{b64(payload)}.fake-signature"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip LOSEIT_* env so the host's real config doesn't leak in."""
    import os

    for k in [k for k in os.environ if k.startswith("LOSEIT_")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── check-token ─────────────────────────────────────────────────────────────


def test_check_token_reports_valid(tmp_path: Path, runner: CliRunner) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(_make_jwt({"sub": "1", "exp": int(time.time()) + 86400}))

    result = runner.invoke(app, ["-o", "json", "check-token", "--token-file", str(token_file)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "check-token"
    assert payload["status"] == "valid"
    assert payload["seconds_until_expiry"] > 0


def test_check_token_reports_expired_with_nonzero_exit(tmp_path: Path, runner: CliRunner) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(_make_jwt({"sub": "1", "exp": int(time.time()) - 86400}))

    result = runner.invoke(app, ["-o", "json", "check-token", "--token-file", str(token_file)])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "expired"
    assert payload["seconds_until_expiry"] < 0


def test_check_token_reports_missing_when_no_file(tmp_path: Path, runner: CliRunner) -> None:
    token_file = tmp_path / "absent-token"  # never created

    result = runner.invoke(app, ["-o", "json", "check-token", "--token-file", str(token_file)])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "missing"
    assert payload["exp"] is None


def test_check_token_text_output_is_human_friendly(tmp_path: Path, runner: CliRunner) -> None:
    """Text mode prints the user-facing CTA on failure (`Run: loseit login ...`)."""
    token_file = tmp_path / "token"  # missing

    result = runner.invoke(app, ["check-token", "--token-file", str(token_file)])
    assert result.exit_code == 1
    # The error CTA points the user (or agent) at the right next step.
    assert "loseit login" in result.output


# ── list-profiles ───────────────────────────────────────────────────────────


@pytest.fixture
def _chrome_profiles_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand up a fake Chrome user-data root with two profiles."""
    user_data = tmp_path / "Chrome"
    (user_data / "Default").mkdir(parents=True)
    (user_data / "Default" / "Cookies").write_text("")
    (user_data / "Profile 2").mkdir()
    (user_data / "Profile 2" / "Cookies").write_text("")
    (user_data / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Default": {"name": "Eric (Personal)"},
                        "Profile 2": {"name": "Eric (Work)"},
                    }
                }
            }
        )
    )

    monkeypatch.setattr(auth_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        auth_module,
        "_COOKIE_GLOBS",
        {"darwin": {"chrome": (f"{user_data}/*/Cookies",)}},
    )
    monkeypatch.setattr(
        auth_module,
        "_USER_DATA_ROOTS",
        {"darwin": {"chrome": (str(user_data),)}},
    )
    return user_data


def test_list_profiles_json_includes_each_profile(
    _chrome_profiles_on_disk: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["-o", "json", "list-profiles", "--browser", "chrome"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "list-profiles"
    assert payload["browser"] == "chrome"
    assert payload["count"] == 2
    by_dir = {p["directory"]: p for p in payload["profiles"]}
    assert by_dir["Default"]["name"] == "Eric (Personal)"
    assert by_dir["Profile 2"]["name"] == "Eric (Work)"


def test_list_profiles_text_renders_directory_and_name(
    _chrome_profiles_on_disk: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["list-profiles"])
    assert result.exit_code == 0, result.output
    # The text view's contract: directory + friendly name so the user can pick.
    assert "Default" in result.output
    assert "Eric (Personal)" in result.output
    assert "Profile 2" in result.output
    # Hints the user at the follow-up flag.
    assert "--profile" in result.output


def test_list_profiles_warns_when_browser_not_installed(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No profiles on disk → friendly message, exit 0 (nothing went wrong)."""
    monkeypatch.setattr(auth_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        auth_module,
        "_COOKIE_GLOBS",
        {"darwin": {"chrome": (f"{tmp_path}/nope/*/Cookies",)}},
    )
    monkeypatch.setattr(
        auth_module,
        "_USER_DATA_ROOTS",
        {"darwin": {"chrome": (f"{tmp_path}/nope",)}},
    )

    result = runner.invoke(app, ["list-profiles"])
    assert result.exit_code == 0
    assert "No Chrome profiles" in result.output


def test_list_profiles_does_not_decrypt_cookies(
    _chrome_profiles_on_disk: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end regression guard: the CLI command stays Keychain-free."""

    def _explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("list-profiles must not decrypt cookies")

    monkeypatch.setattr(auth_module, "load_cookies_from_browser", _explode)
    result = runner.invoke(app, ["list-profiles"])
    assert result.exit_code == 0, result.output
