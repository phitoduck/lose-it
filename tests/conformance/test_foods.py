"""Conformance tests for ``foods.search`` and ``foods.get_unsaved_food_log_entry``."""

from __future__ import annotations

import pytest

from lose_it_utils import Client
from lose_it_utils.client import foods
from lose_it_utils.client._config import Config
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


# ── Regression: long usernames must NOT leak into food name / brand ─────────


@pytest.fixture
def long_username_client(fixture_text, httpx_mock) -> tuple[Client, str]:
    """A Client whose ``user_name`` is longer than any food string in the
    captured fixtures, and whose mocked response has that same long name
    substituted in for the sanitized ``test.user`` placeholder.

    Without the user_name filter in the parser, ``max(by_len)`` picks up the
    username as the food name and the brand picker falls onto it too — the
    exact bug that produced ``brand=eric.riddoch`` entries against the live
    API. This fixture is the controlled reproduction.
    """
    long_name = "extremely-long-username-that-beats-every-brand-string-in-the-fixture"
    cfg = Config(
        user_id="12345678",
        user_name=long_name,
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )
    client = Client(cfg, token="fake-jwt-token")
    return client, long_name


def test_search_filters_long_username_from_name_and_brand(
    long_username_client, httpx_mock, fixture_text
):
    client, long_name = long_username_client
    # Substitute the long username into the captured response where the
    # sanitized "test.user" placeholder sits.
    response_text = fixture_text("search_foods_tortilla.txt").replace("test.user", long_name)
    httpx_mock.add_response(url=SERVICE_URL, text=response_text)

    results = foods.search(client.http, "x-treme carb balance tortilla")

    assert results, "no results parsed"
    for r in results:
        assert r.name != long_name, "username leaked into food name"
        assert r.brand != long_name, "username leaked into food brand"
        assert r.category != long_name, "username leaked into food category"


def test_get_unsaved_filters_long_username_from_name_and_brand(
    long_username_client, httpx_mock, fixture_text
):
    client, long_name = long_username_client
    response_text = fixture_text("get_unsaved_tortilla.txt").replace("test.user", long_name)
    httpx_mock.add_response(url=SERVICE_URL, text=response_text)

    food = FoodSearchResult(
        name="Tortilla Wraps, High Fiber, Low Carb, Xtreme Wellness",
        brand="Mission Tortillas Carb Balance",
        category="Tortilla",
        pk_bytes=list(range(16)),
    )
    unsaved = foods.get_unsaved_food_log_entry(client.http, food)

    assert unsaved.name != long_name, "username leaked into unsaved.name"
    assert unsaved.brand != long_name, "username leaked into unsaved.brand"
    assert unsaved.category != long_name, "username leaked into unsaved.category"


# ── Regression: email-local-part placeholder must not leak into brand ───────


@pytest.fixture
def email_username_client() -> Client:
    """A Client whose ``user_name`` is a full email address.

    Lose It! emits the email-local-part (``test.user``) as a brand-slot
    placeholder for personally-saved foods. With a full-email ``user_name``,
    the parser's equality check ``s != user_name`` misses the placeholder
    unless we also drop the ``@``-prefix.
    """
    cfg = Config(
        user_id="12345678",
        user_name="test.user@example.com",
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )
    return Client(cfg, token="fake-jwt-token")


def test_search_filters_email_local_part_from_brand(
    email_username_client, httpx_mock, fixture_text
):
    """User-saved entries in search results must not carry ``test.user`` as their brand."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("search_foods_with_user_saved.txt"),
    )
    results = foods.search(email_username_client.http, "tomato roasted red pepper soup")
    assert results, "no results parsed"
    for r in results:
        assert r.name != "test.user", f"email-local-part leaked into name: {r!r}"
        assert r.brand != "test.user", f"email-local-part leaked into brand: {r!r}"
        assert r.category != "test.user", f"email-local-part leaked into category: {r!r}"
