"""Foods that ``getFood`` returns without a name.

For many generic database foods ("Almond Butter", "Honey") ``getFood``
returns the full food — category, nutrients, serving sizes — but a null
``FoodIdentifier`` name; only ``searchFoods`` carries it. The fixture
``get_food_nameless_almond_butter.txt`` is a live capture of exactly that.

Before the fix, ``get_food`` treated the missing name as "not found", so
``food_id`` — the only stable way to pin a search hit — failed for these
foods. Now reads work, the search query recovers the name, and writes
refuse a nameless food instead of creating a blank entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest
from typer.testing import CliRunner

from lose_it import LoseIt, UnnamedFoodError
from lose_it.cli import app
from lose_it.core import foods
from lose_it.core._config import Config
from lose_it.core._decoder import decode_response
from lose_it.core._ids import hex_to_pk
from lose_it.core._portion import PortionError

SERVICE_URL = "https://www.loseit.com/web/service"
FIXTURES = Path(__file__).parent / "fixtures"
NAMELESS = (FIXTURES / "get_food_nameless_almond_butter.txt").read_text()
SEARCH = (FIXTURES / "search_foods_tortilla.txt").read_text()
UNSAVED = (FIXTURES / "get_unsaved_tortilla.txt").read_text()
ALMOND_BUTTER_ID = "eb3705446141d09d3c40b1b2002c7ad5"


def _first_search_hit():
    return foods._extract_search_results_from_decoded(decode_response(SEARCH))[0]


def _bodies(httpx_mock) -> list[str]:
    return [unquote(r.content.decode()) for r in httpx_mock.get_requests()]


def test_get_food_returns_nameless_food_instead_of_not_found(test_client, httpx_mock) -> None:
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)
    pk = hex_to_pk(ALMOND_BUTTER_ID)
    food = foods.get_food(test_client.http, pk)
    assert food.name == ""
    assert food.category == "Almond"
    assert food.pk_bytes == pk


def test_get_food_recovers_name_from_query(test_config: Config, httpx_mock) -> None:
    hit = _first_search_hit()
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)  # getFood
    httpx_mock.add_response(url=SERVICE_URL, text=SEARCH)  # searchFoods
    food = LoseIt(test_config, token="x").get_food(hit.food_id, query="tortilla")
    assert food.name == hit.name
    assert food.brand == hit.brand


def test_get_food_skips_search_when_name_present(test_config: Config, httpx_mock) -> None:
    httpx_mock.add_response(url=SERVICE_URL, text=UNSAVED)
    food = LoseIt(test_config, token="x").get_food("00" * 16, query="tortilla")
    assert food.name
    assert not any("searchFoods" in b for b in _bodies(httpx_mock))


def test_log_food_refuses_nameless_food(test_config: Config, httpx_mock) -> None:
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)
    with pytest.raises(UnnamedFoodError, match="search query"):
        LoseIt(test_config, token="x").log_food(ALMOND_BUTTER_ID, dry_run=True)
    assert not any("updateFoodLogEntry" in b for b in _bodies(httpx_mock))


def test_recipe_ingredient_with_food_id_and_query(test_config: Config, httpx_mock) -> None:
    hit = _first_search_hit()
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)  # getFood
    httpx_mock.add_response(url=SERVICE_URL, text=SEARCH)  # searchFoods (name recovery)
    httpx_mock.add_response(url=SERVICE_URL, text=UNSAVED)  # getUnsavedFoodLogEntry
    recipe = LoseIt(test_config, token="x").create_recipe(
        "Wraps", [{"food_id": hit.food_id, "query": "tortilla", "servings": 2}], dry_run=True
    )
    assert recipe.ingredients[0].food.name == hit.name


def test_recipe_ingredient_nameless_without_query_fails_before_save(
    test_config: Config, httpx_mock
) -> None:
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)
    with pytest.raises(ValueError, match=r"ingredient #1: .*no name"):
        LoseIt(test_config, token="x").create_recipe("Wraps", [{"food_id": ALMOND_BUTTER_ID}])
    assert not any("saveRecipe" in b for b in _bodies(httpx_mock))


def test_recipe_portion_error_names_the_ingredient(test_config: Config, httpx_mock) -> None:
    """The tortilla is serving-stored with no mL slot, so 'cup' can't work."""
    httpx_mock.add_response(url=SERVICE_URL, text=SEARCH)
    httpx_mock.add_response(url=SERVICE_URL, text=UNSAVED)
    with pytest.raises(PortionError) as excinfo:
        LoseIt(test_config, token="x").create_recipe(
            "Wraps", [{"query": "tortilla", "serving_amount": 1, "serving_unit": "cup"}]
        )
    assert str(excinfo.value).startswith("ingredient #1: ")
    assert excinfo.value.context["ingredient"] == 1
    assert excinfo.value.context["supported_units"]


# ── CLI ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOSEIT_USER_ID", "12345678")
    monkeypatch.setenv("LOSEIT_USER_NAME", "test.user")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "-6")
    monkeypatch.setenv("LOSEIT_POLICY_HASH", "8F87EC8969F17AE77B6283D3A83F6D4C")
    monkeypatch.setenv("LOSEIT_STRONG_NAME", "351AE5DC0CA36AD3BA9C7CBA7B0E07B8")
    monkeypatch.setenv("LOSEIT_TOKEN", "fake-jwt-token")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def test_cli_log_nameless_food_id_exits_with_hint(env, httpx_mock) -> None:
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)
    result = CliRunner().invoke(
        app, ["-o", "json", "log", "--food-id", ALMOND_BUTTER_ID, "--dry-run"]
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "food_name_unavailable"
    assert payload["food_id"] == ALMOND_BUTTER_ID


def test_cli_log_food_id_with_query_recovers_name(env, httpx_mock) -> None:
    hit = _first_search_hit()
    httpx_mock.add_response(url=SERVICE_URL, text=NAMELESS)  # getFood
    httpx_mock.add_response(url=SERVICE_URL, text=SEARCH)  # searchFoods
    httpx_mock.add_response(url=SERVICE_URL, text=UNSAVED)  # getUnsavedFoodLogEntry
    result = CliRunner().invoke(
        app, ["-o", "json", "log", "tortilla", "--food-id", hit.food_id, "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["food"]["name"] == hit.name
    assert payload["food"]["food_id"] == hit.food_id


def test_cli_describe_food_by_query_and_pick(env, httpx_mock) -> None:
    hit = _first_search_hit()
    httpx_mock.add_response(url=SERVICE_URL, text=SEARCH)
    httpx_mock.add_response(url=SERVICE_URL, text=UNSAVED)
    result = CliRunner().invoke(
        app, ["-o", "json", "describe-food", "--query", "tortilla", "--pick", "1"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 1
    food = payload["foods"][0]
    assert food["food_id"] == hit.food_id
    assert food["supported_units"]
    # Search hit is reused directly — no getFood round-trip.
    assert not any("|getFood|" in b for b in _bodies(httpx_mock))


@pytest.mark.parametrize(
    "args",
    [
        ["describe-food"],
        ["describe-food", ALMOND_BUTTER_ID, "--query", "x"],
        ["describe-food", ALMOND_BUTTER_ID, "--pick", "1"],
    ],
)
def test_cli_describe_food_rejects_bad_selection(env, args: list[str], httpx_mock) -> None:
    result = CliRunner().invoke(app, ["-o", "json", *args])
    assert result.exit_code == 2, result.output
    assert httpx_mock.get_requests() == []
