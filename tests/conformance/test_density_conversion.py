"""Volume ↔ weight conversion via a caller-supplied density (``grams_per_ml``).

Many generic Lose It! entries (honey, cocoa powder, date syrup, nut butters)
store only a volume serving — no ``per_serving_g`` slot — so ``40 g`` of them
is unreachable from the food's own data; the web UI can't do it either. A
density bridges the gap. Without one, the error must say which units *do*
work so the caller can recover in one step.
"""

from __future__ import annotations

import pytest

from lose_it.core._portion import (
    ERR_NON_POSITIVE_GRAMS_PER_ML,
    ERR_UNIT_NOT_SUPPORTED,
    PortionError,
    resolve_portion,
    supported_units,
)
from lose_it.models import UnsavedFoodLogEntry


def _food(**kw) -> UnsavedFoodLogEntry:
    base = dict(
        name="Food",
        brand="",
        category="",
        food_pk_bytes=[1] * 16,
        day_key="Z6mB_lo",
        nutrients={0: 100.0},
        serving_qty=1.0,
        canonical_per_serving=1.0,
        native_qty_per_serving=1.0,
    )
    base.update(kw)
    return UnsavedFoodLogEntry(**base)


def _honey() -> UnsavedFoodLogEntry:
    """Generic 'Honey': 1 tbsp per serving, 14.7868 mL, no gram slot (live wire)."""
    return _food(
        name="Honey",
        nutrients={0: 60.48, 1: 14.7868},
        food_measure_ordinal=2,
        food_measure_unit="tablespoon",
        per_serving_ml=14.7868,
    )


def _hersheys_cocoa() -> UnsavedFoodLogEntry:
    """Hershey's cocoa: 1 tbsp per serving and *no* mL or g slot at all."""
    return _food(
        name="Cocoa Powder, Unsweetened",
        nutrients={0: 10.0},
        food_measure_ordinal=2,
        food_measure_unit="tablespoon",
    )


def _pistachio_butter() -> UnsavedFoodLogEntry:
    """Weight-only food: 100 g per canonical serving, no mL slot."""
    return _food(
        name="Pistachio Butter",
        nutrients={0: 615.0, 2: 100.0},
        food_measure_ordinal=8,
        food_measure_unit="grams",
        native_qty_per_serving=100.0,
        per_serving_g=100.0,
    )


def test_volume_only_food_rejects_grams_and_lists_supported_units() -> None:
    with pytest.raises(PortionError) as excinfo:
        resolve_portion(_honey(), serving_amount=40, serving_unit="g")
    err = excinfo.value
    assert err.code == ERR_UNIT_NOT_SUPPORTED
    assert err.context["supported_units"] == ["tsp", "tbsp", "cup", "fl_oz", "mL"]
    assert err.context["grams_per_ml_would_help"] is True
    assert "grams_per_ml" in str(err)
    assert "tsp, tbsp, cup, fl_oz, mL" in str(err)


def test_density_converts_grams_for_volume_only_food() -> None:
    portion = resolve_portion(_honey(), serving_amount=40, serving_unit="g", grams_per_ml=1.42)
    grams_per_serving = 14.7868 * 1.42
    assert portion.conversion_factor == pytest.approx(grams_per_serving)
    assert portion.canonical_servings == pytest.approx(40 / grams_per_serving)
    assert portion.measure_ord_override == 8
    assert portion.quantity_in_chosen_unit == 40
    assert portion.display_unit == "g"


def test_density_works_from_native_volume_unit_without_ml_slot() -> None:
    """No per_serving_ml stored: mL comes from the static tbsp→mL factor."""
    portion = resolve_portion(
        _hersheys_cocoa(), serving_amount=90, serving_unit="g", grams_per_ml=0.36
    )
    assert portion.conversion_factor == pytest.approx(14.78676478125 * 0.36)


def test_density_converts_volume_for_weight_only_food() -> None:
    portion = resolve_portion(
        _pistachio_butter(), serving_amount=2, serving_unit="tbsp", grams_per_ml=1.0
    )
    # 100 g/serving ÷ 1 g/mL = 100 mL/serving → in tbsp.
    assert portion.conversion_factor == pytest.approx(100 / 14.78676478125)


def test_food_own_conversion_wins_over_density() -> None:
    """A stored gram slot is authoritative; the density is ignored."""
    food = _food(
        nutrients={0: 633.0, 1: 92.4175, 2: 100.0},
        food_measure_ordinal=2,
        canonical_per_serving=0.32,
        native_qty_per_serving=2.0,
        per_serving_g=100.0,
        per_serving_ml=92.4175,
    )
    with_density = resolve_portion(food, serving_amount=60, serving_unit="g", grams_per_ml=5.0)
    without = resolve_portion(food, serving_amount=60, serving_unit="g")
    assert with_density.conversion_factor == without.conversion_factor == 100.0


def test_teaspoon_reachable_through_ml_slot() -> None:
    """Serving-stored food with an mL slot: tsp used to be missing from the mL fan-out."""
    food = _food(food_measure_ordinal=27, food_measure_unit="serving", per_serving_ml=10.0)
    portion = resolve_portion(food, serving_amount=1, serving_unit="tsp")
    assert portion.conversion_factor == pytest.approx(10.0 / 4.92892159375)


def test_serving_only_food_has_no_density_hint() -> None:
    """Neither volume nor weight known: a density can't help, so don't suggest it."""
    food = _food(food_measure_ordinal=27, food_measure_unit="serving")
    with pytest.raises(PortionError) as excinfo:
        resolve_portion(food, serving_amount=25, serving_unit="g", grams_per_ml=1.0)
    assert excinfo.value.context["supported_units"] == ["serving"]
    assert excinfo.value.context["grams_per_ml_would_help"] is False


def test_non_positive_density_rejected() -> None:
    with pytest.raises(PortionError) as excinfo:
        resolve_portion(_honey(), serving_amount=40, serving_unit="g", grams_per_ml=0)
    assert excinfo.value.code == ERR_NON_POSITIVE_GRAMS_PER_ML


@pytest.mark.parametrize(
    ("food", "expected"),
    [
        (_honey(), ["tsp", "tbsp", "cup", "fl_oz", "mL"]),
        (_hersheys_cocoa(), ["tsp", "tbsp", "cup", "fl_oz", "mL"]),
        (_pistachio_butter(), ["g"]),
    ],
)
def test_supported_units(food: UnsavedFoodLogEntry, expected: list[str]) -> None:
    assert supported_units(food) == expected
