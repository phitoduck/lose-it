"""Shared pytest fixtures.

Provides:

- ``fixture_path(name)`` — resolves a filename under ``tests/conformance/fixtures``.
- ``fixture_text(name)`` — reads a captured GWT-RPC response body as text.
- ``test_config`` — a :class:`Config` populated with the sanitized placeholders
  used by the captured fixtures (so request bodies in unit tests match the
  byte-for-byte shape of real captures).
- ``test_client`` — a :class:`Client` whose httpx transport is mocked via the
  ``pytest-httpx`` ``httpx_mock`` fixture; tests register canned responses to
  ``/web/service`` and assert on what the SDK sends + parses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lose_it import Client
from lose_it.core._config import Config

FIXTURE_DIR = Path(__file__).parent / "conformance" / "fixtures"


@pytest.fixture
def fixture_path():
    def _path(name: str) -> Path:
        return FIXTURE_DIR / name

    return _path


@pytest.fixture
def fixture_text():
    def _text(name: str) -> str:
        return (FIXTURE_DIR / name).read_text()

    return _text


@pytest.fixture
def test_config() -> Config:
    """A Config matching the sanitized placeholders in the captured fixtures."""
    return Config(
        user_id="12345678",
        user_name="test.user",
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )


@pytest.fixture
def test_client(test_config: Config) -> Client:
    """A Client that posts to httpx_mock (per-test mocks via ``httpx_mock``)."""
    return Client(test_config, token="fake-jwt-token")
