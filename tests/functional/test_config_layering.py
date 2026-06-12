"""End-to-end functional tests for the 12-factor config chain.

These tests invoke the actual ``loseit`` Typer app via :class:`CliRunner`
and confirm that the resolved config (printed by ``whoami``) matches the
expected layering. No real API is touched — ``whoami`` only reads
config; it does not hit the backend — so these tests run unconditionally,
unlike the GWT-RPC tests marked ``requires_auth`` (skipped without ``--run-auth``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lose_it.cli import app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every LOSEIT_* env var so the test env is hermetic."""
    import os

    for k in [k for k in os.environ if k.startswith("LOSEIT_")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the SDK at a throwaway token file so `whoami` doesn't need a real JWT."""
    f = tmp_path / "token"
    f.write_text("fake-jwt-token")
    monkeypatch.setenv("LOSEIT_TOKEN", "fake-jwt-token")
    return f


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


# ── YAML alone ──────────────────────────────────────────────────────────────


def test_whoami_reads_from_yaml(
    tmp_path: Path,
    runner: CliRunner,
    token_file: Path,
) -> None:
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "9999"
user_name: yaml-only
hours_from_gmt: -3
policy_hash: YAML_POLICY_HASH_PADDING_ABCDEFGH
strong_name: YAML_STRONG_NAME_PADDING_ABCDEFGH
""",
    )
    result = runner.invoke(
        app,
        ["whoami", "--config-file", str(yaml_file), "-o", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["user_id"] == "9999"
    assert payload["user_name"] == "yaml-only"
    assert payload["hours_from_gmt"] == -3
    assert payload["policy_hash"] == "YAML_POLICY_HASH_PADDING_ABCDEFGH"


# ── Env overrides YAML ──────────────────────────────────────────────────────


def test_env_overrides_yaml_via_cli(
    tmp_path: Path,
    runner: CliRunner,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "1"
user_name: yaml-user
hours_from_gmt: 0
""",
    )
    monkeypatch.setenv("LOSEIT_USER_NAME", "env-user")
    result = runner.invoke(
        app,
        ["whoami", "--config-file", str(yaml_file), "-o", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["user_id"] == "1"  # from YAML
    assert payload["user_name"] == "env-user"  # env beat YAML


# ── CLI overrides env + YAML ────────────────────────────────────────────────


def test_cli_flag_beats_env_and_yaml(
    tmp_path: Path,
    runner: CliRunner,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "yaml-id"
user_name: yaml-user
hours_from_gmt: 0
policy_hash: YAML_POLICY
""",
    )
    monkeypatch.setenv("LOSEIT_USER_NAME", "env-user")
    monkeypatch.setenv("LOSEIT_POLICY_HASH", "ENV_POLICY")
    result = runner.invoke(
        app,
        [
            "whoami",
            "--config-file",
            str(yaml_file),
            "--user-name",
            "cli-user",
            "--policy-hash",
            "CLI_POLICY",
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["user_id"] == "yaml-id"  # YAML
    assert payload["user_name"] == "cli-user"  # CLI > env > YAML
    assert payload["policy_hash"] == "CLI_POLICY"  # CLI > env > YAML


# ── LOSEIT_CONFIG_FILE env var picks the YAML ───────────────────────────────


def test_yaml_path_from_env_var(
    tmp_path: Path,
    runner: CliRunner,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_file = _write_yaml(
        tmp_path / "via-env.yaml",
        """
user_id: "via-env"
user_name: via-env-user
hours_from_gmt: -2
""",
    )
    monkeypatch.setenv("LOSEIT_CONFIG_FILE", str(yaml_file))
    result = runner.invoke(app, ["whoami", "-o", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["user_id"] == "via-env"
    assert payload["user_name"] == "via-env-user"
    assert payload["hours_from_gmt"] == -2


# ── Missing config produces a friendly error ────────────────────────────────


def test_missing_required_config_exits_nonzero(
    runner: CliRunner,
    tmp_path: Path,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No YAML, no env vars, no CLI flags → exit code 2 + helpful message."""
    # Point config_file at a nonexistent path so no YAML contributes.
    monkeypatch.setenv("LOSEIT_CONFIG_FILE", str(tmp_path / "nope.yaml"))
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "Missing required setting" in combined or "user_id" in combined


# ── CLI flag alone (no YAML, no env) ────────────────────────────────────────


def test_all_required_via_cli_flags_only(
    runner: CliRunner,
    tmp_path: Path,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOSEIT_CONFIG_FILE", str(tmp_path / "nope.yaml"))
    result = runner.invoke(
        app,
        [
            "whoami",
            "--user-id",
            "cli-only",
            "--user-name",
            "cli-only-user",
            "--hours-from-gmt",
            "-1",
            "-o",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["user_id"] == "cli-only"
    assert payload["user_name"] == "cli-only-user"
    assert payload["hours_from_gmt"] == -1
