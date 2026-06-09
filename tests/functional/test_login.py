"""Functional tests for ``lose-it login`` — browser → token round-trip.

Exercises :func:`lose_it_utils.client.auth.refresh_token_from_browser` against
the real Chrome and Brave cookie stores on the developer's laptop. For each
browser the test will:

1. Read the live ``liauth`` cookie via ``browser-cookie3``.
2. Verify it parses as a JWT and that ``exp`` is in the future.
3. Build a Client with that token and make a read-only ``foods.search`` call
   to confirm the token actually authenticates against the live API.

The test parametrizes over ``("chrome", "brave")`` and uses ``pytest.skip``
(not failure) when a particular browser isn't installed or isn't logged in,
so contributors who only use one of the two still see the other one run.

Skipped entirely unless ``LOSEIT_RUN_FUNCTIONAL=1`` is set, since reading the
cookie store on macOS triggers a Keychain prompt and we don't want CI or
unrelated test runs poking at the user's keychain.
"""

from __future__ import annotations

import os
import time
from typing import Literal

import pytest

from lose_it_utils import Client
from lose_it_utils.client import foods
from lose_it_utils.client._settings import Settings
from lose_it_utils.client.auth import (
    decode_jwt_exp,
    is_token_expired,
    refresh_token_from_browser,
)

pytestmark = pytest.mark.functional


@pytest.fixture(autouse=True)
def _gate() -> None:
    if os.environ.get("LOSEIT_RUN_FUNCTIONAL") != "1":
        pytest.skip("functional test gated on LOSEIT_RUN_FUNCTIONAL=1")


@pytest.mark.parametrize("browser", ["chrome", "brave"])
def test_login_extracts_valid_token_and_authenticates(
    browser: Literal["chrome", "brave"],
) -> None:
    """``refresh_token_from_browser`` returns a JWT that the live API accepts."""
    token = refresh_token_from_browser(browser)
    if token is None:
        pytest.skip(
            f"no liauth cookie found in {browser}; sign in at loseit.com in "
            f"{browser.title()} to enable this test"
        )

    # 1. Shape: a real liauth is a 3-segment ES384 JWT.
    parts = token.split(".")
    assert len(parts) == 3, f"expected 3-segment JWT, got {len(parts)} segments"
    exp = decode_jwt_exp(token)
    assert exp is not None, "JWT payload had no `exp` claim"
    assert exp > time.time(), (
        f"liauth cookie in {browser} is already expired (exp={exp}, now={time.time():.0f}); "
        f"re-sign-in at loseit.com in {browser.title()} and re-run."
    )
    assert not is_token_expired(token)

    # 2. The token actually authenticates: search the public food DB and
    #    require at least one result. We pick a generic query ("apple") so
    #    this stays stable across food-DB changes.
    #
    #    `Client.from_env` reads LOSEIT_* env vars / YAML for the user_id +
    #    user_name + hours_from_gmt the GWT-RPC body requires; the `token`
    #    kwarg below short-circuits the on-disk token file so this test
    #    measures the *browser-extracted* token specifically.
    settings = Settings()  # type: ignore[call-arg]  # validator enforces required fields
    with Client(settings, token=token) as client:
        results = foods.search(client.http, "apple")

    assert results, f"foods.search returned 0 results with the {browser} token"
