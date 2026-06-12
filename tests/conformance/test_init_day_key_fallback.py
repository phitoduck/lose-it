"""Conformance: ``get_daydate_key`` always returns a non-empty key.

The Lose It! server's ``getDailyDetailsIncludingPendingForDate`` RPC
requires a non-empty ``day_key`` in the payload, but only validates
``day_num`` to resolve which diary day to return. The init RPC only
echoes back day_keys for the ~recent window (~30 days exact, plus a
sparse weekly history extending back ~2 years).

Without this fallback, asking for any historical date outside that
window raised ``HTTP 500: The call failed on the server`` because we
sent ``day_key=""``. The fallback uses a placeholder shaped like a
real key (``ZZZZZZZ``); the server accepts it and returns the data
for the requested ``day_num``.

Verified empirically on 2026-06-12: sending ``key="XXXXXX"``,
``"ABCDEFG"``, or ``"Z6mB_lo"`` (a real but unrelated key) all return
byte-identical responses to the real key for that day.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from lose_it.client.init import _FALLBACK_DAY_KEY, get_daydate_key


def test_fallback_key_is_non_empty_and_well_shaped() -> None:
    """The placeholder must be non-empty and look like a real day_key.

    Day keys observed on the wire are 4-16 chars, drawn from
    [A-Za-z0-9_$]. The placeholder follows this shape so the server's
    payload validator sees something well-formed; the *content* is
    ignored (the server routes on day_num alone).
    """
    assert _FALLBACK_DAY_KEY
    assert 4 <= len(_FALLBACK_DAY_KEY) <= 16
    assert re.match(r"^[A-Za-z0-9_$]+$", _FALLBACK_DAY_KEY)


def test_get_daydate_key_uses_fallback_when_target_not_in_init_window() -> None:
    """When the init RPC's day_key window doesn't contain target_day_num,
    we return ``_FALLBACK_DAY_KEY`` rather than ``None`` or ``""``.

    Simulates the historical-diary case (querying e.g. day_num=8932 for
    2025-06-15 when the init response only includes day_nums 8583-9293
    sparsely).
    """
    fake_tokens = (["totally", "unrelated", "tokens", "no", "day_nums"], None)

    class _FakeHttp:
        class config:
            hours_from_gmt = -6

        @staticmethod
        def post_rpc(_payload: str) -> str:
            return "<irrelevant — parse_response is mocked>"

    with (
        patch("lose_it.client.init.build_payload", return_value="<stub>"),
        patch("lose_it.client.init.parse_response", return_value=fake_tokens),
    ):
        result = get_daydate_key(_FakeHttp(), target_day_num=99999)

    assert result == _FALLBACK_DAY_KEY


def test_get_daydate_key_returns_exact_match_when_target_in_init_window() -> None:
    """When the init response contains the target day_num, return its key.

    The fallback only kicks in for misses; exact matches still win.
    """
    # Two adjacent tokens of the shape (day_num, day_key) that the
    # scanner should find.
    fake_tokens = (
        ["leading", "noise", 9291, "Z6rLlVo", "trailing", "noise"],
        None,
    )

    class _FakeHttp:
        class config:
            hours_from_gmt = -6

        @staticmethod
        def post_rpc(_payload: str) -> str:
            return "<irrelevant — parse_response is mocked>"

    with (
        patch("lose_it.client.init.build_payload", return_value="<stub>"),
        patch("lose_it.client.init.parse_response", return_value=fake_tokens),
    ):
        result = get_daydate_key(_FakeHttp(), target_day_num=9291)

    assert result == "Z6rLlVo"
