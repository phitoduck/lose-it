"""Conformance tests for ``daily.get_daily_details`` (parse + request shape)."""

from __future__ import annotations

from datetime import date

from lose_it.client import daily
from lose_it.client._models import FoodLogEntry

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


def test_daily_details_filters_email_local_part_from_brand(fixture_text):
    """User-saved foods without a real brand must not leak the user's email-local-part.

    When a user logs a personal/customized food whose original brand isn't
    preserved, the Lose It! server inserts the logging user's email-local-part
    (e.g. ``test.user`` for ``test.user@example.com``) as a placeholder string
    in the brand_ref slot. If ``parse_entries`` filters only against the full
    configured ``user_name``, the placeholder leaks into ``food_brand`` and
    bumps the actual food_name into the food_category field.
    """
    text = fixture_text("get_daily_details_with_user_saved_food.txt")
    # Caller's user_name is the full email; the placeholder is the local-part.
    entries = daily.parse_entries(
        text,
        default_hours_from_gmt=-6,
        user_name="test.user@example.com",
    )
    assert entries, "no entries parsed"

    # The placeholder string must never appear in either brand or name.
    for e in entries:
        assert e.food_brand != "test.user", (
            f"email-local-part leaked into food_brand for {e.food_name!r}"
        )
        assert e.food_name != "test.user", (
            f"email-local-part leaked into food_name for {e.food_brand!r}"
        )

    # The user-saved soup + Kodiak entries had no server-side brand, so after
    # filtering the placeholder out, food_brand is empty and food_name + category
    # reflect the real strings (not shifted by one).
    by_name = {e.food_name: e for e in entries}
    assert "Organic Tomatoe & Roasted Red Pepper Soup" in by_name, (
        f"expected the user-saved soup entry; got names={list(by_name)}"
    )
    soup = by_name["Organic Tomatoe & Roasted Red Pepper Soup"]
    assert soup.food_brand == "", f"expected empty brand, got {soup.food_brand!r}"
    assert soup.food_category == "Tomato", f"expected category='Tomato', got {soup.food_category!r}"
