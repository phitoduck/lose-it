"""Conformance tests for the bulk-range diary RPC.

Covers the new :func:`lose_it.core.daily.build_range_payload` /
:func:`get_daily_details_range` pair and the high-level
:meth:`lose_it.LoseIt.diary_range` composition. The wire shape is pinned
against captured fixtures so any drift in the GWT-RPC envelope shows up
as a byte-match failure.
"""

from __future__ import annotations

from datetime import date

import pytest

from lose_it import LoseIt
from lose_it.core.daily import (
    TooMuchData,
    build_range_payload,
    get_daily_details_range,
)
from lose_it.models import FoodLogEntry

SERVICE_URL = "https://www.loseit.com/web/service"


def test_build_range_payload_byte_matches_captured_request(test_config, fixture_text):
    """Envelope build is byte-for-byte equal to the captured fixture."""
    body = build_range_payload(
        test_config,
        start_day_num=9290,
        start_day_key="Z6mB_lo",
        end_day_num=9296,
        end_day_key="Z7E7iFo",
    )
    expected = fixture_text("get_daily_details_for_date_range_request.txt").rstrip("\n")
    assert body == expected


def test_decode_range_response_yields_seven_dates(test_client, httpx_mock, fixture_text):
    """The captured response decodes into 7 dates (2026-06-08 through 2026-06-14).

    The fixture's day_nums 9290..9296 map to 2026-06-08..2026-06-14 via
    the in-package anchor; the per-day FLE counts are
    [7, 4, 6, 9, 5, 0, 0] in date order.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_for_date_range_response.txt"),
    )
    result = get_daily_details_range(
        test_client.http,
        start=date(2026, 6, 8),
        end=date(2026, 6, 14),
        day_keys={9290: "Z6mB_lo", 9296: "Z7E7iFo"},
    )
    assert len(result) == 7
    expected_dates = [date(2026, 6, d) for d in range(8, 15)]
    assert sorted(result.keys()) == expected_dates
    # Each value is a list (possibly empty) of FoodLogEntry.
    for entries in result.values():
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, FoodLogEntry)

    # Per-day FLE counts pinned against the captured fixture.
    counts = [len(result[d]) for d in expected_dates]
    assert counts == [7, 4, 6, 9, 5, 0, 0]

    # Exactly one RPC went out — the range call. No per-day fan-out.
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 1
    body = reqs[0].content.decode()
    assert "getDailyDetailsIncludingPendingForDateRange" in body


def test_decode_range_response_returns_proper_food_log_entries(
    test_client, httpx_mock, fixture_text
):
    """Entries decoded from the range response have the same shape as single-day."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_for_date_range_response.txt"),
    )
    result = get_daily_details_range(
        test_client.http,
        start=date(2026, 6, 8),
        end=date(2026, 6, 14),
        day_keys={9290: "Z6mB_lo", 9296: "Z7E7iFo"},
    )
    flat = [e for entries in result.values() for e in entries]
    assert flat, "expected at least one FoodLogEntry decoded"
    for entry in flat:
        assert len(entry.food_pk_response) == 16
        assert len(entry.entry_pk_response) == 16
        assert entry.nutrients_ordered, "expected nutrient values"


def test_oversize_response_raises_too_much_data(test_client, httpx_mock):
    """HTTP 413 from the server → :class:`TooMuchData`."""
    httpx_mock.add_response(url=SERVICE_URL, status_code=413, text="oversize")
    with pytest.raises(TooMuchData):
        get_daily_details_range(
            test_client.http,
            start=date(2024, 12, 1),
            end=date(2024, 12, 31),
        )


def test_rate_limited_response_raises_too_much_data(test_client, httpx_mock):
    """HTTP 429 also raises :class:`TooMuchData`."""
    httpx_mock.add_response(url=SERVICE_URL, status_code=429, text="rate limited")
    with pytest.raises(TooMuchData):
        get_daily_details_range(
            test_client.http,
            start=date(2024, 12, 1),
            end=date(2024, 12, 31),
        )


def test_server_error_raises_too_much_data(test_client, httpx_mock):
    """HTTP 5xx — the server tripped — also raises :class:`TooMuchData`."""
    httpx_mock.add_response(url=SERVICE_URL, status_code=503, text="upstream timeout")
    with pytest.raises(TooMuchData):
        get_daily_details_range(
            test_client.http,
            start=date(2024, 12, 1),
            end=date(2024, 12, 31),
        )


def test_diary_range_on_client_composes_init_cache(test_config, httpx_mock, fixture_text):
    """``LoseIt.diary_range`` issues one init RPC then one range RPC — that's it."""
    httpx_mock.add_response(url=SERVICE_URL, text=fixture_text("get_initialization_data.txt"))
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_for_date_range_response.txt"),
    )

    li = LoseIt(test_config, token="fake-jwt-token")
    result = li.diary_range(start=date(2026, 6, 8), end=date(2026, 6, 14))
    assert len(result) == 7

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2, f"expected 2 RPCs (init + range), got {len(reqs)}"
    init_body = reqs[0].content.decode()
    range_body = reqs[1].content.decode()
    assert "getInitializationData" in init_body
    assert "getDailyDetailsIncludingPendingForDateRange" in range_body


def test_diary_range_reuses_cached_init_on_subsequent_calls(test_config, httpx_mock, fixture_text):
    """Second call to ``diary_range`` does NOT re-issue the init RPC."""
    httpx_mock.add_response(url=SERVICE_URL, text=fixture_text("get_initialization_data.txt"))
    # Two range responses for two range calls.
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_for_date_range_response.txt"),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_for_date_range_response.txt"),
    )

    li = LoseIt(test_config, token="fake-jwt-token")
    li.diary_range(start=date(2026, 6, 8), end=date(2026, 6, 14))
    li.diary_range(start=date(2026, 6, 8), end=date(2026, 6, 14))

    reqs = httpx_mock.get_requests()
    # 1 init + 2 ranges, total 3. No second init.
    assert len(reqs) == 3
    methods = [
        "getInitializationData" if "getInitializationData" in r.content.decode() else "range"
        for r in reqs
    ]
    assert methods.count("getInitializationData") == 1
    assert methods.count("range") == 2


def test_get_daily_details_range_rejects_end_before_start(test_client):
    """A reversed range is a user error, not a server one."""
    with pytest.raises(ValueError):
        get_daily_details_range(
            test_client.http,
            start=date(2026, 6, 14),
            end=date(2026, 6, 8),
        )
