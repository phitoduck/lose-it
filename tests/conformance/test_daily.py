"""Conformance tests for ``daily.get_daily_details`` (parse + request shape)."""

from __future__ import annotations

from datetime import date

from lose_it_utils.client import daily
from lose_it_utils.client._models import FoodLogEntry

SERVICE_URL = "https://www.loseit.com/web/service"


def test_daily_details_request_envelope(test_client, httpx_mock, fixture_text):
    """The getDailyDetails request encodes the target date + day key."""
    # 2 responses: getInitializationData (for day_key lookup) then daily details.
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_initialization_data.txt"),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_with_tortilla.txt"),
    )
    daily.get_daily_details(test_client.http, date(2026, 6, 8))

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    # The second request is the daily-details call.
    body = reqs[1].content.decode()
    assert "getDailyDetailsIncludingPendingForDate" in body
    # Day number 9290 corresponds to 2026-06-08 per the in-package anchor.
    assert "|9290|" in body


def test_daily_details_parses_food_log_entries(fixture_text):
    """``parse_entries`` extracts every FoodLogEntry from the captured fixture."""
    text = fixture_text("get_daily_details_with_tortilla.txt")
    entries = daily.parse_entries(text, default_hours_from_gmt=-6)
    assert entries, "no entries parsed"
    for e in entries:
        assert isinstance(e, FoodLogEntry)
        assert len(e.food_pk_response) == 16
        assert len(e.entry_pk_response) == 16
        assert e.food_identifier_code.startswith("Do")
        # Each entry must have at least one nutrient and a non-empty food name.
        assert e.nutrients_ordered, "expected nutrient values"
        assert "ortilla" in e.food_name
    # All entries in this capture were logged to snacks.
    assert all(e.meal_ordinal == 3 for e in entries)


def test_daily_details_after_delete_omits_target(fixture_text):
    """The post-delete fixture has strictly fewer 'tortilla' entries than before."""
    before = daily.parse_entries(fixture_text("get_daily_details_with_tortilla.txt"))
    after = daily.parse_entries(fixture_text("get_daily_details_after_delete.txt"))
    before_tortillas = [e for e in before if "ortilla" in e.food_name]
    after_tortillas = [e for e in after if "ortilla" in e.food_name]
    assert len(after_tortillas) == len(before_tortillas) - 1, (
        f"expected one entry removed; before={len(before_tortillas)}, after={len(after_tortillas)}"
    )


def test_daily_details_entries_have_unique_entry_pks(fixture_text):
    """Sanity: every parsed entry's UUID-style entry PK is unique within the diary."""
    text = fixture_text("get_daily_details_with_tortilla.txt")
    entries = daily.parse_entries(text)
    entry_pks = [tuple(e.entry_pk_response) for e in entries]
    assert len(entry_pks) == len(set(entry_pks))
