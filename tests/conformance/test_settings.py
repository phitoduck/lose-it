"""Unit tests for the layered :class:`Settings` loader.

Exercises every layer of the 12-factor priority chain:

    CLI / init kwargs  >  LOSEIT_* env vars  >  YAML file  >  field defaults

Each test starts from a clean env (every ``LOSEIT_*`` cleared via the
autouse ``_clean_env`` fixture) so test order and the developer's shell
can't affect outcomes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lose_it_utils.client._settings import (
    BASE_URL,
    SERVICE_URL,
    MissingConfigError,
    Settings,
    load_settings,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every LOSEIT_* env var so tests start from a known state."""
    import os

    for k in [k for k in os.environ if k.startswith("LOSEIT_")]:
        monkeypatch.delenv(k, raising=False)


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


# ── Defaults layer ──────────────────────────────────────────────────────────


def test_defaults_apply_when_required_supplied_via_init(tmp_path: Path) -> None:
    """With required fields passed but optional ones absent, defaults apply."""
    yaml_file = tmp_path / "noexist.yaml"  # missing — YAML source returns nothing
    s = Settings(
        config_file=yaml_file,
        user_id="111",
        user_name="alice",
        hours_from_gmt=-5,
    )
    assert s.base_url == BASE_URL
    assert s.service_url == SERVICE_URL
    assert s.policy_hash == "8F87EC8969F17AE77B6283D3A83F6D4C"
    assert s.strong_name == "351AE5DC0CA36AD3BA9C7CBA7B0E07B8"


def test_missing_required_fields_raise(tmp_path: Path) -> None:
    """No source supplies the account-identifier triple → MissingConfigError."""
    yaml_file = tmp_path / "empty.yaml"
    yaml_file.write_text("")
    with pytest.raises(MissingConfigError) as exc:
        load_settings(config_file=yaml_file)
    msg = str(exc.value)
    assert "user_id" in msg
    assert "user_name" in msg
    assert "hours_from_gmt" in msg


# ── YAML layer ──────────────────────────────────────────────────────────────


def test_yaml_only_supplies_all_fields(tmp_path: Path) -> None:
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "111"
user_name: yaml-user
hours_from_gmt: -7
policy_hash: YAML_POLICY
strong_name: YAML_STRONG
""",
    )
    s = load_settings(config_file=yaml_file)
    assert s.user_id == "111"
    assert s.user_name == "yaml-user"
    assert s.hours_from_gmt == -7
    assert s.policy_hash == "YAML_POLICY"
    assert s.strong_name == "YAML_STRONG"


def test_missing_yaml_file_is_not_an_error(tmp_path: Path) -> None:
    """A nonexistent YAML path is treated as "no contribution", not an error."""
    s = load_settings(
        config_file=tmp_path / "nope.yaml",
        user_id="x",
        user_name="y",
        hours_from_gmt=0,
    )
    assert s.user_id == "x"


# ── Env layer ───────────────────────────────────────────────────────────────


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "111"
user_name: yaml-user
hours_from_gmt: -7
""",
    )
    monkeypatch.setenv("LOSEIT_USER_NAME", "env-user")
    s = load_settings(config_file=yaml_file)
    assert s.user_id == "111"  # from YAML
    assert s.user_name == "env-user"  # env beats YAML
    assert s.hours_from_gmt == -7  # from YAML


def test_env_supplies_required_when_yaml_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOSEIT_USER_ID", "222")
    monkeypatch.setenv("LOSEIT_USER_NAME", "env-only")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "-6")
    s = load_settings(config_file=Path("/nonexistent/path"))
    assert s.user_id == "222"
    assert s.user_name == "env-only"
    assert s.hours_from_gmt == -6


# ── CLI / init layer (highest) ──────────────────────────────────────────────


def test_init_kwargs_override_env_and_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "111"
user_name: yaml-user
hours_from_gmt: -7
policy_hash: YAML_POLICY
""",
    )
    monkeypatch.setenv("LOSEIT_USER_NAME", "env-user")
    monkeypatch.setenv("LOSEIT_POLICY_HASH", "ENV_POLICY")
    s = load_settings(
        config_file=yaml_file,
        user_name="cli-user",
        policy_hash="CLI_POLICY",
    )
    assert s.user_id == "111"  # YAML
    assert s.user_name == "cli-user"  # CLI > env > YAML
    assert s.policy_hash == "CLI_POLICY"  # CLI > env > YAML


def test_none_overrides_are_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_settings(user_name=None)`` must NOT shadow the env/YAML value."""
    yaml_file = _write_yaml(
        tmp_path / "cfg.yaml",
        """
user_id: "111"
user_name: yaml-user
hours_from_gmt: -7
""",
    )
    monkeypatch.setenv("LOSEIT_USER_NAME", "env-user")
    s = load_settings(config_file=yaml_file, user_name=None, policy_hash=None)
    assert s.user_name == "env-user"  # env still wins, None was discarded


# ── config_file resolution ──────────────────────────────────────────────────


def test_config_file_resolved_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = _write_yaml(
        tmp_path / "from-env.yaml",
        """
user_id: "env-yaml-id"
user_name: env-yaml-name
hours_from_gmt: 0
""",
    )
    monkeypatch.setenv("LOSEIT_CONFIG_FILE", str(yaml_file))
    s = load_settings()
    assert s.user_id == "env-yaml-id"


def test_config_file_cli_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _write_yaml(
        tmp_path / "a.yaml",
        """
user_id: from-a
user_name: a
hours_from_gmt: 1
""",
    )
    b = _write_yaml(
        tmp_path / "b.yaml",
        """
user_id: from-b
user_name: b
hours_from_gmt: 2
""",
    )
    monkeypatch.setenv("LOSEIT_CONFIG_FILE", str(a))
    s = load_settings(config_file=b)
    assert s.user_id == "from-b"  # CLI config_file path wins


# ── Hours coercion ──────────────────────────────────────────────────────────


def test_hours_from_gmt_string_coerced_to_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env vars arrive as strings; the int field must coerce."""
    monkeypatch.setenv("LOSEIT_USER_ID", "1")
    monkeypatch.setenv("LOSEIT_USER_NAME", "u")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "-8")
    s = load_settings(config_file=Path("/nope"))
    assert s.hours_from_gmt == -8
    assert isinstance(s.hours_from_gmt, int)


# ── Immutability ────────────────────────────────────────────────────────────


def test_settings_are_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOSEIT_USER_ID", "1")
    monkeypatch.setenv("LOSEIT_USER_NAME", "u")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "0")
    s = load_settings(config_file=Path("/nope"))
    with pytest.raises((TypeError, ValueError, Exception)):
        s.user_id = "999"  # type: ignore[misc]
