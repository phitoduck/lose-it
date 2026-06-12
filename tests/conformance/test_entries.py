"""Conformance tests for ``entries.log_food`` and ``entries.delete``."""

from __future__ import annotations

from lose_it.client import daily, entries
from lose_it.client._gwt import parse_response
from lose_it.client._models import UnsavedFoodLogEntry

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


def test_log_food_grams_portion_uses_food_native_qty_per_serving(
    test_client, httpx_mock, fixture_text
):
    """FoodServingSize portion_size is ``servings × native_qty_per_serving``.

    Previously the CLI hardcoded "1 serving = 100 g" for any gram-measured
    food. That overcounted for foods like Built Bar (40 g/serving) and
    undercounted for foods like protein powders (28-47 g/serving). The
    fix reads f4/f3 from the unsaved-entry response and uses the food's
    actual per-serving qty.

    Here a food where ``native_qty_per_serving=100`` at ``--servings 1.2``
    correctly serializes portion_size = 120 g — same answer as the legacy
    hardcoded path for the lucky case where f4 happens to equal 100.
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
        canonical_per_serving=1.0,
        native_qty_per_serving=100.0,  # this food: 1 serving = 100 g
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
    assert "|28|8|1|" in body
    # 1.2 servings × 100 g/serving = 120 g.
    assert "|120|1|28|8|" in body, body[body.find("28|8") - 40 : body.find("28|8") + 40]
    assert "|1.2|" in body


def test_log_food_grams_uses_food_specific_per_serving_qty(test_client, httpx_mock, fixture_text):
    """A Built-Bar-shaped food (40 g/serving) at --servings 2 emits 80 g.

    Smoking-gun regression: the legacy --grams 100g convention applied
    blindly here would have logged ``200 g`` for a food that's actually
    40 g/serving. The fix honors the food's own f4/f3.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("update_food_log_entry_success.txt"),
    )
    unsaved = UnsavedFoodLogEntry(
        name="Puff Protein Bar",
        brand="Built",
        category="Bars",
        food_pk_bytes=[2] * 16,
        day_key="Z6mB_lo",
        nutrients={0: 175.0},
        serving_qty=1.0,
        food_measure_ordinal=8,  # grams
        canonical_per_serving=1.0,
        native_qty_per_serving=40.0,  # this food: 1 serving = 40 g
    )
    entries.log_food(
        test_client.http,
        unsaved,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=2.0,
    )
    body = httpx_mock.get_request().content.decode()
    # 2.0 servings × 40 g/serving = 80 g.
    assert "|80|1|28|8|" in body, body[body.find("28|8") - 40 : body.find("28|8") + 40]
    assert "|2|" in body


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
