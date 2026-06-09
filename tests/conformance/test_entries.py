"""Conformance tests for ``entries.log_food`` and ``entries.delete``."""

from __future__ import annotations

from lose_it_utils.client import daily, entries
from lose_it_utils.client._gwt import parse_response
from lose_it_utils.client._models import UnsavedFoodLogEntry

SERVICE_URL = "https://www.loseit.com/web/service"


def test_log_food_request_shape(test_client, httpx_mock, fixture_text):
    """``log_food`` sends an updateFoodLogEntry envelope with all required fields."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("update_food_log_entry_success.txt"),
    )
    unsaved = UnsavedFoodLogEntry(
        name="Test Food",
        brand="Test Brand",
        category="TestCategory",
        food_pk_bytes=[1] * 16,
        day_key="Z6mB_lo",
        nutrients={0: 100.0, 2: 5.0, 3: 1.0, 8: 0.0, 9: 50.0, 10: 10.0, 11: 2.0, 12: 5.0, 13: 8.0},
        serving_qty=1.0,
        food_measure_ordinal=27,
    )
    entries.log_food(
        test_client.http,
        unsaved,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=1.5,
    )

    body = httpx_mock.get_request().content.decode()
    assert "updateFoodLogEntry" in body
    # ServiceRequestToken always carries user_id + hours_from_gmt.
    assert f"|{test_client.config.user_id}|" in body
    assert f"|{test_client.config.hours_from_gmt}|" in body
    # Meal ordinal 3 = snacks must show up in the entry-type slot.
    assert "|21|3|0|" in body
    # The food's day key + day number get serialized into the context.
    assert "|Z6mB_lo|9290|" in body
    # FoodMeasure ordinal 27 (Serving) is sent rather than the default 45.
    assert "|28|27|1|" in body


def test_delete_request_shape(test_client, httpx_mock, fixture_text):
    """``entries.delete`` serializes the full FoodLogEntry back to the server."""
    text = fixture_text("get_daily_details_with_tortilla.txt")
    diary_entries = daily.parse_entries(text, default_hours_from_gmt=-6)
    assert diary_entries
    target = diary_entries[0]

    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("delete_food_log_entry_success.txt"),
    )
    entries.delete(test_client.http, target)

    body = httpx_mock.get_request().content.decode()
    assert "deleteFoodLogEntry" in body
    # Both the food PK and the entry PK must appear (reversed) in the wire body.
    food_pk_wire = "|".join(str(int(b)) for b in reversed(target.food_pk_response))
    entry_pk_wire = "|".join(str(int(b)) for b in reversed(target.entry_pk_response))
    assert food_pk_wire in body
    assert entry_pk_wire in body
    # The food's day key + day number get embedded in the context.
    assert f"|{target.context_day_key}|{target.day_num}|" in body
    # The food identifier code (DoXxxx) goes into the FoodServingSize section.
    assert f"|{target.food_identifier_code}|" in body


def test_log_food_grams_serializes_portion_in_grams(test_client, httpx_mock, fixture_text):
    """For gram-measured foods (ord=8) the FoodServingSize quantity is the
    literal gram count, not the servings multiplier.

    Regression: previously ``servings=1.2`` on a gram-measured entry shipped
    quantity=1.2 in the FoodServingSize slot, which the official Lose It! UI
    rendered as "1.2 grams" instead of "120 grams". This asserts the wire
    body now carries the literal portion size in grams (servings × 100).
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("update_food_log_entry_success.txt"),
    )
    unsaved = UnsavedFoodLogEntry(
        name="Chicken Strips",
        brand="Real Good Foods",
        category="Chicken",
        food_pk_bytes=[1] * 16,
        day_key="Z6mB_lo",
        nutrients={0: 130.0},
        serving_qty=1.0,
        food_measure_ordinal=8,  # grams
    )
    entries.log_food(
        test_client.http,
        unsaved,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=1.2,
    )
    body = httpx_mock.get_request().content.decode()
    # FoodMeasure ord=8 still appears: "|28|8|1|".
    assert "|28|8|1|" in body
    # The FoodServingSize quantity slot must contain 120 (= 1.2 × 100), not 1.2.
    # The substring "|120|1|28|8|" is the expected
    # "FoodServingSize.quantity|<sep>|FoodMeasure-ref|measure-ord|" sequence.
    assert "|120|1|28|8|" in body, body[body.find("28|8") - 40 : body.find("28|8") + 40]
    # And the bare "1.2" still appears as the FoodServing.quantity (# of servings).
    assert "|1.2|" in body


def test_log_food_non_gram_food_unchanged(test_client, httpx_mock, fixture_text):
    """For non-gram-measured foods (e.g. ord=27 Serving) quantity = servings."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("update_food_log_entry_success.txt"),
    )
    unsaved = UnsavedFoodLogEntry(
        name="Tortilla",
        brand="Mission",
        category="Tortilla",
        food_pk_bytes=[1] * 16,
        day_key="Z6mB_lo",
        nutrients={0: 70.0},
        serving_qty=1.0,
        food_measure_ordinal=27,
    )
    entries.log_food(
        test_client.http,
        unsaved,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=1.5,
    )
    body = httpx_mock.get_request().content.decode()
    # For ord=27, servings (1.5) is sent as-is in the FoodServingSize slot.
    assert "|1.5|1|28|27|" in body


def test_delete_response_parses_as_ok(fixture_text):
    """The captured delete success response parses to a non-error //OK envelope."""
    text = fixture_text("delete_food_log_entry_success.txt")
    _tokens, strings = parse_response(text)
    assert text.startswith("//OK[")
    # The response carries a UserId + Integer + Boolean shape — verify strings.
    assert any("UserId/" in s for s in strings)
    assert any("Integer/" in s for s in strings)
    assert any("Boolean/" in s for s in strings)
