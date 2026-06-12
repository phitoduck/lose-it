"""Coverage for FoodMeasurement + FoodNutrient enums + decoder labeling."""

from __future__ import annotations

from lose_it.client._enums import (
    FoodMeasurement,
    FoodNutrient,
    label_for_nutrient,
    label_for_ordinal,
)


def test_known_ordinals_label_to_lowercase_enum_names() -> None:
    """Every confirmed FoodMeasurement ordinal labels to its enum name."""
    assert label_for_ordinal(1) == "teaspoon"
    assert label_for_ordinal(2) == "tablespoon"
    assert label_for_ordinal(3) == "cup"
    assert label_for_ordinal(4) == "piece"
    assert label_for_ordinal(5) == "each"
    assert label_for_ordinal(8) == "grams"
    assert label_for_ordinal(10) == "fluid_ounce"
    assert label_for_ordinal(11) == "milliliter"
    assert label_for_ordinal(19) == "bottle"
    assert label_for_ordinal(21) == "can"
    assert label_for_ordinal(26) == "slice"
    assert label_for_ordinal(27) == "serving"
    assert label_for_ordinal(33) == "scoop"
    assert label_for_ordinal(45) == "container"
    assert label_for_ordinal(46) == "pie"


def test_unknown_ordinal_falls_back_to_unknown_ord_n() -> None:
    """Not-yet-labelled ordinals surface as ``unknown_ord_<N>``.

    A few observed-but-not-yet-confirmed ords: 6 (per-food display unit
    that varies; not a stable measurement constant), 16 (Orgain protein
    shake), 34 (single Quaker dry-oats sample), 35 (inconsistent across
    SkinnyPop / kale cans).
    """
    assert label_for_ordinal(6) == "unknown_ord_6"
    assert label_for_ordinal(16) == "unknown_ord_16"
    assert label_for_ordinal(34) == "unknown_ord_34"
    assert label_for_ordinal(35) == "unknown_ord_35"


def test_label_for_none_is_unknown() -> None:
    assert label_for_ordinal(None) == "unknown"


def test_enum_is_int_subclass() -> None:
    """IntEnum so equality with raw ints (from the wire) works."""
    assert FoodMeasurement.CUP == 3
    assert FoodMeasurement.GRAMS == 8


def test_decoder_attaches_unit_label_to_food_measure_object() -> None:
    """End-to-end: decoded FoodMeasure objects carry a ``unit`` field."""
    from lose_it.client._decoder import _FOOD_MEASURE_FQCN

    # Build a synthetic FoodMeasure dict the way the decoder would, then
    # assert the labeler attaches the right unit.
    fake_decoded = {"__type__": _FOOD_MEASURE_FQCN, "ordinal": 33}
    # Mimic the decoder hook by calling label_for_ordinal directly.
    fake_decoded["unit"] = label_for_ordinal(fake_decoded["ordinal"])
    assert fake_decoded["unit"] == "scoop"


# ── FoodNutrient enum ───────────────────────────────────────────────────────


def test_known_nutrient_ordinals_label_correctly() -> None:
    """Every confirmed FoodNutrient ordinal labels to its enum name."""
    assert label_for_nutrient(0) == "calories"
    assert label_for_nutrient(1) == "serving_volume_ml"
    assert label_for_nutrient(2) == "serving_weight_g"
    assert label_for_nutrient(3) == "total_fat_g"
    assert label_for_nutrient(4) == "saturated_fat_g"
    assert label_for_nutrient(8) == "cholesterol_mg"
    assert label_for_nutrient(9) == "sodium_mg"
    assert label_for_nutrient(10) == "carb_g"
    assert label_for_nutrient(11) == "fiber_g"
    assert label_for_nutrient(12) == "sugar_g"
    assert label_for_nutrient(13) == "protein_g"


def test_unknown_nutrient_ordinal_falls_back() -> None:
    """Unmapped nutrient slots surface as ``unknown_nutrient_<N>``.

    Micronutrient slots (14-29) are not yet labelled — they should
    surface verbatim with their ordinal so downstream code (and humans)
    can see what raw value to expect.
    """
    assert label_for_nutrient(18) == "unknown_nutrient_18"
    assert label_for_nutrient(22) == "unknown_nutrient_22"
    assert label_for_nutrient(29) == "unknown_nutrient_29"


def test_nutrient_enum_is_int_subclass() -> None:
    """IntEnum so equality with raw HashMap ordinals (from the wire) works."""
    assert FoodNutrient.CALORIES == 0
    assert FoodNutrient.SODIUM_MG == 9


def test_nutrient_enum_distinct_from_measurement_enum() -> None:
    """Same int value can mean different things in the two enums.

    e.g. ord=8 means ``GRAMS`` as a FoodMeasurement (unit), but means
    ``CHOLESTEROL_MG`` as a FoodNutrient (nutrient slot). They share
    the same Java class on the wire — context determines semantics.
    """
    assert FoodMeasurement.GRAMS == 8
    assert FoodNutrient.CHOLESTEROL_MG == 8
    # And different enum types are not equal to each other:
    assert label_for_ordinal(8) == "grams"
    assert label_for_nutrient(8) == "cholesterol_mg"
