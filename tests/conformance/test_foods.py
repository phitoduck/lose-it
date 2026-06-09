"""Conformance tests for ``foods.search`` and ``foods.get_unsaved_food_log_entry``."""

from __future__ import annotations

from lose_it_utils.client import foods
from lose_it_utils.client._models import FoodSearchResult

SERVICE_URL = "https://www.loseit.com/web/service"


def test_search_request_envelope(test_client, httpx_mock, fixture_text):
    """The search request body has the correct GWT envelope and method ref."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("search_foods_tortilla.txt"),
    )
    results = foods.search(test_client.http, "x-treme carb balance tortilla")

    sent = httpx_mock.get_request()
    body = sent.content.decode()
    assert "searchFoods" in body
    assert "x-treme carb balance tortilla" in body
    assert body.startswith("7|0|")
    # Should be parsed as a list of FoodSearchResult
    assert isinstance(results, list)


def test_search_parses_captured_response(test_client, httpx_mock, fixture_text):
    """Parsing the captured search response yields recognizable tortilla results."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("search_foods_tortilla.txt"),
    )
    results = foods.search(test_client.http, "x-treme carb balance tortilla")
    assert results, "no results parsed"
    # At least one result should look like a Mission Carb Balance entry.
    assert any("Xtreme" in r.name or "Mission" in r.brand or "Mission" in r.name for r in results)
    # Every result has 16-byte PK bytes.
    for r in results:
        assert isinstance(r, FoodSearchResult)
        assert len(r.pk_bytes) == 16


def test_get_unsaved_request_envelope(test_client, httpx_mock, fixture_text):
    """The unsaved-entry request body contains the food name + reversed PK bytes."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_unsaved_tortilla.txt"),
    )
    food = FoodSearchResult(
        name="Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness",
        brand="Mission Tortillas Carb Balance",
        category="Tortilla",
        pk_bytes=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    )
    foods.get_unsaved_food_log_entry(test_client.http, food)

    sent = httpx_mock.get_request()
    body = sent.content.decode()
    assert "getUnsavedFoodLogEntry" in body
    # PK bytes appear REVERSED on the wire
    assert "|16|15|14|13|12|11|10|9|8|7|6|5|4|3|2|1|" in body
    # Locale + food name appear at the end of the payload
    assert "|en-US|" in body
    assert "|Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness|" in body


def test_get_unsaved_parses_captured_response(test_client, httpx_mock, fixture_text):
    """Parsing the unsaved-entry response yields a usable template with nutrients."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_unsaved_tortilla.txt"),
    )
    food = FoodSearchResult(
        name="Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness",
        brand="Mission Tortillas Carb Balance",
        category="Tortilla",
        pk_bytes=list(range(16)),
    )
    unsaved = foods.get_unsaved_food_log_entry(test_client.http, food)
    assert "Tortilla" in unsaved.name
    assert unsaved.food_pk_bytes is not None
    assert len(unsaved.food_pk_bytes) == 16
    # Calories + key macros should land within the core 9-nutrient set the
    # server accepts. (Not every food has all 9 — e.g. tortilla has no Fat.)
    assert 0 in unsaved.nutrients  # Calories
    assert 60 <= unsaved.nutrients[0] <= 80
    # All nutrient ordinals must be in the documented 0..30 range.
    for ord_ in unsaved.nutrients:
        assert 0 <= ord_ <= 30
