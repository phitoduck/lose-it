"""Tests for ``loseit version`` + ``loseit --version``.

These exercise the CLI through Typer's :class:`CliRunner`, so they hit the
same code path real users do. No network, no auth — runs in the default suite.

The expected version is loaded from ``version.txt`` at the repo root so the
test stays correct across bumps (CI fails on tag collisions, so the file is
always the live truth).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lose_it.cli import app

VERSION_TXT = Path(__file__).resolve().parents[2] / "version.txt"


@pytest.fixture
def expected_version() -> str:
    return VERSION_TXT.read_text().strip()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _assert_text_output(output: str, expected_version: str) -> None:
    """Every required field shows up in the human-readable form."""
    assert f"loseit {expected_version}" in output
    assert f"https://github.com/phitoduck/lose-it/releases/tag/v{expected_version}" in output, (
        "release URL should include the v-prefixed semver tag"
    )
    assert "License: MIT" in output
    assert "unaffiliated with Lose It! / FitNow, Inc." in output
    assert "Thank you for using it!" in output


def test_version_subcommand_text(runner: CliRunner, expected_version: str) -> None:
    """`loseit version` prints semver, release URL, license, disclaimer, thanks."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    _assert_text_output(result.output, expected_version)


def test_version_flag_text(runner: CliRunner, expected_version: str) -> None:
    """`loseit --version` prints the same payload as the subcommand and exits 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    _assert_text_output(result.output, expected_version)


def test_version_subcommand_json(runner: CliRunner, expected_version: str) -> None:
    """`--output json version` emits a structured envelope."""
    result = runner.invoke(app, ["version", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "version": expected_version,
        "release_url": (f"https://github.com/phitoduck/lose-it/releases/tag/v{expected_version}"),
        "license": "MIT",
        "disclaimer": "This project is unaffiliated with Lose It! / FitNow, Inc.",
        "thanks": "Thank you for using it!",
    }


def test_version_flag_does_not_require_config(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--version` short-circuits before config/auth resolution.

    Strip every LOSEIT_* env var so any accidental config lookup would
    explode loudly; ``--version`` must still exit cleanly.
    """
    import os

    for key in [k for k in os.environ if k.startswith("LOSEIT_")]:
        monkeypatch.delenv(key, raising=False)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
