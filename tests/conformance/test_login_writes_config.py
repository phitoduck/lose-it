"""Tests for ``loseit login`` populating the YAML config file.

Covers the unit-level YAML writer (`write_yaml_config`) and the
end-to-end CLI flow that takes a browser cookie → JWT → YAML config so
the user doesn't have to set any ``LOSEIT_*`` env vars manually.

The browser cookie store is mocked at module boundary
(`refresh_token_from_browser` / `load_cookies_from_browser`) so the
tests run without a real Chrome/Brave install and without poking the
macOS Keychain.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from lose_it.cli import app
from lose_it.core import auth as auth_module
from lose_it.core._settings import write_yaml_config


def _make_jwt(payload: dict[str, Any]) -> str:
    header = {"alg": "ES384", "typ": "JWT", "kid": "TESTKID"}

    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64(header)}.{b64(payload)}.fake-signature"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for k in [k for k in os.environ if k.startswith("LOSEIT_")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── write_yaml_config ───────────────────────────────────────────────────────


def test_write_yaml_config_creates_new_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.yaml"
    written = write_yaml_config(target, {"user_id": "1", "user_name": "alice"})

    assert written == target
    assert target.exists()
    loaded = yaml.safe_load(target.read_text())
    assert loaded == {"user_id": "1", "user_name": "alice"}


def test_write_yaml_config_merges_with_existing(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text(
        "user_id: '1'\nuser_name: alice\npolicy_hash: USER_SET_HASH\n",
    )

    write_yaml_config(target, {"user_name": "alice-renamed", "hours_from_gmt": -5})

    loaded = yaml.safe_load(target.read_text())
    assert loaded["user_id"] == "1"  # preserved
    assert loaded["user_name"] == "alice-renamed"  # overwritten
    assert loaded["hours_from_gmt"] == -5  # added
    assert loaded["policy_hash"] == "USER_SET_HASH"  # preserved


def test_write_yaml_config_chmods_600(tmp_path: Path) -> None:
    """YAML may end up with a `token` field later; ensure restrictive perms."""
    import os
    import stat

    target = tmp_path / "config.yaml"
    write_yaml_config(target, {"user_id": "1"})
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


# ── login command — happy path: user_name found in JWT ──────────────────────


def test_login_writes_config_from_jwt_email(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = _make_jwt(
        {
            "sub": "42424242",
            "email": "alice@example.com",
            "exp": int(time.time()) + 86400,
        }
    )
    monkeypatch.setattr(auth_module, "refresh_token_from_browser", lambda _b: jwt)
    monkeypatch.setattr("lose_it.client.refresh_token_from_browser", lambda _b: jwt)

    token_file = tmp_path / "token"
    config_file = tmp_path / "config.yaml"

    result = runner.invoke(
        app,
        [
            "login",
            "--browser",
            "chrome",
            "--token-file",
            str(token_file),
            "--write-config-to",
            str(config_file),
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["config_file"] == str(config_file)
    assert payload["config_values"]["user_id"] == "42424242"
    assert payload["config_values"]["user_name"] == "alice@example.com"
    assert isinstance(payload["config_values"]["hours_from_gmt"], int)

    on_disk = yaml.safe_load(config_file.read_text())
    assert on_disk["user_id"] == "42424242"
    assert on_disk["user_name"] == "alice@example.com"
    assert "hours_from_gmt" in on_disk

    # Token file written too.
    assert token_file.read_text().strip() == jwt


# ── login command — fall back to cookie sniff when JWT lacks user_name ──────


def test_login_uses_cookie_when_jwt_has_no_email(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = _make_jwt({"sub": "777", "exp": int(time.time()) + 86400})
    monkeypatch.setattr("lose_it.client.refresh_token_from_browser", lambda _b: jwt)
    monkeypatch.setattr(
        "lose_it.core._login_flow.load_cookies_from_browser",
        lambda _b: {"loseit_email": "via-cookie@example.com", "other": "x"},
    )

    token_file = tmp_path / "token"
    config_file = tmp_path / "config.yaml"
    result = runner.invoke(
        app,
        [
            "login",
            "--browser",
            "chrome",
            "--token-file",
            str(token_file),
            "--write-config-to",
            str(config_file),
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_values"]["user_id"] == "777"
    assert payload["config_values"]["user_name"] == "via-cookie@example.com"


# ── login command — --user-name flag wins over everything ───────────────────


def test_user_name_flag_beats_jwt_and_cookies(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = _make_jwt(
        {
            "sub": "1",
            "email": "from-jwt@example.com",
            "exp": int(time.time()) + 86400,
        }
    )
    monkeypatch.setattr("lose_it.client.refresh_token_from_browser", lambda _b: jwt)
    monkeypatch.setattr(
        "lose_it.core._login_flow.load_cookies_from_browser",
        lambda _b: {"loseit_email": "from-cookie@example.com"},
    )

    token_file = tmp_path / "token"
    config_file = tmp_path / "config.yaml"
    result = runner.invoke(
        app,
        [
            "login",
            "--browser",
            "chrome",
            "--user-name",
            "from-cli@example.com",
            "--token-file",
            str(token_file),
            "--write-config-to",
            str(config_file),
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_values"]["user_name"] == "from-cli@example.com"


# ── login command — --no-write-config skips YAML ────────────────────────────


def test_no_write_config_skips_yaml(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = _make_jwt(
        {
            "sub": "1",
            "email": "x@example.com",
            "exp": int(time.time()) + 86400,
        }
    )
    monkeypatch.setattr("lose_it.client.refresh_token_from_browser", lambda _b: jwt)

    token_file = tmp_path / "token"
    config_file = tmp_path / "config.yaml"
    result = runner.invoke(
        app,
        [
            "login",
            "--browser",
            "chrome",
            "--token-file",
            str(token_file),
            "--write-config-to",
            str(config_file),
            "--no-write-config",
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config_file"] is None
    assert payload["config_values"] is None
    assert not config_file.exists()


# ── login command — JSON-mode without discoverable user_name skips YAML ─────


def test_json_mode_with_unresolvable_user_name_skips_yaml(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON output mode is non-interactive; never prompt or block."""
    jwt = _make_jwt({"sub": "1", "exp": int(time.time()) + 86400})
    monkeypatch.setattr("lose_it.client.refresh_token_from_browser", lambda _b: jwt)
    monkeypatch.setattr("lose_it.core._login_flow.load_cookies_from_browser", lambda _b: {})

    token_file = tmp_path / "token"
    config_file = tmp_path / "config.yaml"
    result = runner.invoke(
        app,
        [
            "login",
            "--browser",
            "chrome",
            "--token-file",
            str(token_file),
            "--write-config-to",
            str(config_file),
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["config_file"] is None  # nothing written
    assert not config_file.exists()


# ── login command — existing YAML keys are preserved ────────────────────────


def test_login_preserves_unrelated_yaml_keys(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-set policy_hash mustn't be wiped out by `loseit login`."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "policy_hash: USER_PINNED_HASH\nstrong_name: USER_PINNED_STRONG\n",
    )

    jwt = _make_jwt(
        {
            "sub": "1",
            "email": "x@example.com",
            "exp": int(time.time()) + 86400,
        }
    )
    monkeypatch.setattr("lose_it.client.refresh_token_from_browser", lambda _b: jwt)

    result = runner.invoke(
        app,
        [
            "login",
            "--browser",
            "chrome",
            "--token-file",
            str(tmp_path / "token"),
            "--write-config-to",
            str(config_file),
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output

    on_disk = yaml.safe_load(config_file.read_text())
    assert on_disk["policy_hash"] == "USER_PINNED_HASH"
    assert on_disk["strong_name"] == "USER_PINNED_STRONG"
    assert on_disk["user_id"] == "1"
    assert on_disk["user_name"] == "x@example.com"
