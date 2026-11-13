"""Conformance tests for recipe creation (``saveRecipe``).

Ground truth is ``save_recipe_web_capture.txt``: the official web client's
outbound ``saveRecipe`` body for an existing 8-ingredient recipe (captured
2026-11-12, permutation 201A8433…, account identifiers sanitized). The
tests pin (a) that our schema-driven request reader understands that
capture end to end, and (b) that the payload ``build_save_recipe_payload``
emits has the same object graph.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import pytest
from typer.testing import CliRunner

from lose_it import LoseIt, RecipeIngredientSpec
from lose_it.cli import app
from lose_it.core._config import Config
from lose_it.core._decoder import _decode_long
from lose_it.core.recipes import (
    SERVING_MEASURE_ORDINAL,
    IngredientLine,
    _hour_date,
    build_save_recipe_payload,
    encode_long,
    recipe_totals,
)
from lose_it.models import UnsavedFoodLogEntry
from tests._gwt_request import BackRef, Enum, parse_request

SERVICE_URL = "https://www.loseit.com/web/service"
FIXTURES = Path(__file__).parent / "fixtures"

RECIPE_PK = list(range(-8, 8))
INGREDIENT_PKS = [list(range(16)), list(range(16, 32))]
NOW = datetime(2026, 11, 12, 15, 47, 20, tzinfo=UTC)


def _egg() -> UnsavedFoodLogEntry:
    # Shaped like the web capture's "Egg, Large": 100 g per serving = 2 each.
    return UnsavedFoodLogEntry(
        name="Egg, Large",
        brand="",
        category="Egg",
        food_pk_bytes=list(range(100, 116)),
        day_key="aHQdpaN",
        nutrients={0: 143.0, 3: 9.51, 13: 12.56},
        food_measure_ordinal=5,
        canonical_per_serving=0.5,
        native_qty_per_serving=1.0,
    )


def _oil() -> UnsavedFoodLogEntry:
    return UnsavedFoodLogEntry(
        name="Oil, Olive",
        brand="Bertolli",
        category="Oil",
        food_pk_bytes=list(range(-60, -44)),
        day_key="",
        nutrients={0: 119.0, 3: 13.5},
        food_measure_ordinal=2,
        canonical_per_serving=1.0,
        native_qty_per_serving=1.0,
    )


def _lines() -> list[IngredientLine]:
    return [
        # 3 eggs = 1.5 servings — the exact FoodServingSize the web sent.
        IngredientLine(_egg(), 1.5, 5, 0.5, 1.0, 3.0),
        IngredientLine(_oil(), 0.5, 2, 1.0, 1.0, 0.5),
    ]


def _build(config: Config, **kw) -> str:
    return build_save_recipe_payload(
        config,
        kw.pop("name", "Test Omelette"),
        kw.pop("lines", _lines()),
        kw.pop("servings", 2),
        recipe_pk=RECIPE_PK,
        ingredient_pks=INGREDIENT_PKS,
        now=NOW,
    )


# ── Ground truth: the web client's own payload ──────────────────────────────


def test_web_capture_parses_completely() -> None:
    req = parse_request((FIXTURES / "save_recipe_web_capture.txt").read_text())
    assert req.method == "saveRecipe"
    assert req.tokens_consumed == req.tokens_total == 1272
    recipe = req.params[1]
    assert recipe.type == "Recipe"
    assert recipe[0][1][3] == "Asian Prawns Stir Fry"  # ActiveFood → FoodIdentifier name
    assert len(recipe[6]) == 8  # ingredients


def test_web_capture_ingredient_serving_shape() -> None:
    """Pins the semantics the builder relies on: per-ingredient FoodServingSize
    is (servings, true, unit, d, e, quantity) with quantity in that unit."""
    req = parse_request((FIXTURES / "save_recipe_web_capture.txt").read_text())
    onion = req.params[1][6][0]
    assert onion[1][3] == "Onion, Yellow, Chopped"
    serving_size = onion[2][1]
    assert [serving_size[0], serving_size[5]] == ["1.2", "120"]  # 1.2 servings = 120 g


# ── Builder ─────────────────────────────────────────────────────────────────


def test_payload_parses_completely(test_config: Config) -> None:
    req = parse_request(_build(test_config))
    assert req.service == "com.loseit.core.client.service.LoseItRemoteService"
    assert req.method == "saveRecipe"
    assert req.tokens_consumed == req.tokens_total
    token = req.params[0]
    assert token[1][0] == test_config.user_id
    assert token[1][1] == test_config.user_name


def test_payload_recipe_header(test_config: Config) -> None:
    recipe = parse_request(_build(test_config)).params[1]
    active_food = recipe[0]
    identifier = active_food[1]
    assert (identifier[1], identifier[3]) == ("Recipe", "Test Omelette")
    assert identifier[9][0] == RECIPE_PK
    # FoodServing: whole-recipe totals over (1, 1), serving size "N Servings".
    nutrients, serving_size = active_food[2][0], active_food[2][1]
    assert (nutrients[0], nutrients[1]) == ("1", "1")
    totals = {k.ordinal: v for k, v in nutrients[2]}
    assert totals[0] == pytest.approx(143 * 1.5 + 119 * 0.5)
    assert serving_size[2] == Enum("FoodMeasure", SERVING_MEASURE_ORDINAL)
    assert [serving_size[0], serving_size[3], serving_size[4], serving_size[5]] == [
        "1",
        "1",
        "2",
        "2",
    ]
    assert recipe[4] == "2"  # serving quantity
    assert recipe[11] == Enum("FoodMeasure", 27)
    assert recipe[14][0] == RECIPE_PK
    assert not any(isinstance(v, BackRef) for v in recipe.fields.values())


def test_payload_ingredients(test_config: Config) -> None:
    recipe = parse_request(_build(test_config)).params[1]
    egg, oil = recipe[6]
    assert egg.type == "RecipeIngredient"
    identifier = egg[1]
    assert (identifier[1], identifier[2], identifier[3]) == ("Egg", "en-US", "Egg, Large")
    assert identifier[4] is None  # no brand → null, like the web client
    assert identifier[7] == Enum("Verification", 0)
    assert identifier[9][0] == list(reversed(range(100, 116)))  # food PK reversed on the wire
    nutrients, serving_size = egg[2][0], egg[2][1]
    assert (nutrients[0], nutrients[1]) == ("1", "1.5")  # per-serving map × 1.5
    assert {k.ordinal: v for k, v in nutrients[2]}[0] == 143.0
    assert serving_size[2] == Enum("FoodMeasure", 5)
    assert [serving_size[0], serving_size[3], serving_size[4], serving_size[5]] == [
        "1.5",
        "0.5",
        "1",
        "3",
    ]
    assert egg[6][0] == RECIPE_PK  # parent recipe
    assert egg[8][0] == INGREDIENT_PKS[0]
    assert oil[1][4] == "Bertolli"
    assert oil[1][8] == "A"  # empty day_key → zero long


def test_payload_hour_date_matches_web_capture() -> None:
    # The capture sent HourDate(Date "aHNDd_o", 226704) on 2026-11-12.
    assert _hour_date(NOW.date()) == ("aHNDd_o", 226704)


@pytest.mark.parametrize("raw", ["aHNDd_o", "aHQcSwi", "aE_TxII", "A"])
def test_encode_long_round_trips(raw: str) -> None:
    assert encode_long(_decode_long(raw)) == raw


def test_recipe_totals_sums_every_ordinal() -> None:
    totals = recipe_totals(_lines())
    assert totals[0] == pytest.approx(274.0)
    assert totals[3] == pytest.approx(9.51 * 1.5 + 13.5 * 0.5)
    assert totals[13] == pytest.approx(12.56 * 1.5)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"name": "  "}, "name"),
        ({"servings": 0}, "servings"),
        ({"lines": []}, "ingredient"),
    ],
)
def test_builder_rejects_bad_input(test_config: Config, kw: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _build(test_config, **kw)


# ── RecipeIngredientSpec ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"query": "   "},
        {"query": "egg", "grams_per_ml": 0},
        {"query": "egg", "pick": 0},
        {"query": "egg", "servings": -1},
    ],
)
def test_ingredient_spec_rejects(data: dict) -> None:
    with pytest.raises(ValueError):
        RecipeIngredientSpec.from_dict(data)


def test_ingredient_spec_accepts_food_id_with_query_and_density() -> None:
    spec = RecipeIngredientSpec.from_dict(
        {"query": "honey", "food_id": "00" * 16, "grams_per_ml": "1.42"}
    )
    assert spec.food_id == "00" * 16
    assert spec.query == "honey"
    assert spec.grams_per_ml == 1.42


def test_ingredient_spec_ignores_descriptive_keys() -> None:
    spec = RecipeIngredientSpec.from_dict(
        {"query": "egg", "serving_amount": 3, "serving_unit": "each", "original": "3 eggs"}
    )
    assert spec.to_dict() == {
        "query": "egg",
        "pick": 1,
        "servings": 1.0,
        "serving_amount": 3.0,
        "serving_unit": "each",
    }


# ── LoseIt.create_recipe (mocked backend) ───────────────────────────────────


def _mock_tortilla(httpx_mock, times: int = 1) -> None:
    for _ in range(times):
        httpx_mock.add_response(
            url=SERVICE_URL, text=(FIXTURES / "search_foods_tortilla.txt").read_text()
        )
        httpx_mock.add_response(
            url=SERVICE_URL, text=(FIXTURES / "get_unsaved_tortilla.txt").read_text()
        )


def _bodies(httpx_mock) -> list[str]:
    return [unquote(r.content.decode()) for r in httpx_mock.get_requests()]


def test_create_recipe_dry_run_skips_save(test_config: Config, httpx_mock) -> None:
    _mock_tortilla(httpx_mock)
    li = LoseIt(test_config, token="x")
    recipe = li.create_recipe(
        "Wraps", [{"query": "tortilla", "servings": 2}], servings=2, dry_run=True
    )
    assert recipe.dry_run is True
    assert recipe.ingredients[0].canonical_servings == 2
    assert recipe.total_calories == pytest.approx(140)
    assert recipe.calories_per_serving == pytest.approx(70)
    assert not any("|saveRecipe|" in b for b in _bodies(httpx_mock))


def test_create_recipe_posts_save_recipe(test_config: Config, httpx_mock) -> None:
    _mock_tortilla(httpx_mock, times=2)
    httpx_mock.add_response(url=SERVICE_URL, text="//OK[[],0,7]")
    li = LoseIt(test_config, token="x")
    li.create_recipe(
        "Wraps",
        [{"query": "tortilla", "servings": 2}, {"query": "tortilla", "pick": 1}],
        servings=3,
    )
    saves = [b for b in _bodies(httpx_mock) if "|saveRecipe|" in b]
    assert len(saves) == 1
    req = parse_request(saves[0])
    assert req.tokens_consumed == req.tokens_total
    recipe = req.params[1]
    assert recipe[0][1][3] == "Wraps"
    assert recipe[4] == "3"
    assert [ing[2][0][1] for ing in recipe[6]] == ["2", "1"]


def test_create_recipe_resolves_everything_before_saving(test_config: Config, httpx_mock) -> None:
    """A bad pick on ingredient #2 must abort before saveRecipe is sent."""
    _mock_tortilla(httpx_mock)
    httpx_mock.add_response(
        url=SERVICE_URL, text=(FIXTURES / "search_foods_tortilla.txt").read_text()
    )
    li = LoseIt(test_config, token="x")
    with pytest.raises(ValueError, match="ingredient #2"):
        li.create_recipe("Wraps", [{"query": "tortilla"}, {"query": "tortilla", "pick": 99}])
    assert not any("|saveRecipe|" in b for b in _bodies(httpx_mock))


# ── CLI ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOSEIT_USER_ID", "12345678")
    monkeypatch.setenv("LOSEIT_USER_NAME", "test.user")
    monkeypatch.setenv("LOSEIT_HOURS_FROM_GMT", "-6")
    monkeypatch.setenv("LOSEIT_TOKEN", "fake-jwt-token")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def test_cli_add_recipe_dry_run_json(env, tmp_path: Path, httpx_mock) -> None:
    _mock_tortilla(httpx_mock)
    spec = tmp_path / "recipe.json"
    spec.write_text(
        json.dumps(
            {
                "name": "Wraps",
                "servings": 2,
                "ingredients": [{"query": "tortilla", "servings": 2, "original": "2 wraps"}],
            }
        )
    )
    result = CliRunner().invoke(app, ["-o", "json", "add-recipe", str(spec), "--dry-run"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "add-recipe"
    assert payload["dry_run"] is True
    assert payload["name"] == "Wraps"
    assert payload["calories_per_serving"] == pytest.approx(70)
    assert payload["ingredients"][0]["servings"] == 2
    assert not any("|saveRecipe|" in b for b in _bodies(httpx_mock))


def test_cli_add_recipe_reads_stdin_and_overrides(env, httpx_mock) -> None:
    _mock_tortilla(httpx_mock)
    spec = json.dumps({"name": "ignored", "ingredients": [{"query": "tortilla"}]})
    result = CliRunner().invoke(
        app,
        ["-o", "json", "add-recipe", "-", "--name", "From Flag", "--servings", "4", "--dry-run"],
        input=spec,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["name"], payload["servings"]) == ("From Flag", 4)


@pytest.mark.parametrize(
    "spec",
    [
        "not json",
        json.dumps({"ingredients": [{"query": "egg"}]}),  # no name
        json.dumps({"name": "X", "ingredients": []}),
        json.dumps({"name": "X", "ingredients": [{"query": "egg", "serving_amount": 2}]}),
        json.dumps({"name": "X", "servings": 0, "ingredients": [{"query": "egg"}]}),
    ],
)
def test_cli_add_recipe_rejects_bad_spec_without_http(env, spec: str, httpx_mock) -> None:
    result = CliRunner().invoke(app, ["-o", "json", "add-recipe", "-"], input=spec)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"] == "invalid_spec"
    assert httpx_mock.get_requests() == []
