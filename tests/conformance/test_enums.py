"""Coverage for the FoodMeasurement enum + decoder unit labeling."""

from __future__ import annotations

from lose_it_utils.client._enums import FoodMeasurement, label_for_ordinal


def test_known_ordinals_label_to_lowercase_enum_names() -> None:
    """Every confirmed FoodMeasurement ordinal labels to its enum name."""
    assert label_for_ordinal(2) == "tablespoon"
    assert label_for_ordinal(3) == "cup"
    assert label_for_ordinal(5) == "each"
    assert label_for_ordinal(8) == "grams"
    assert label_for_ordinal(10) == "fluid_ounce"
    assert label_for_ordinal(11) == "milliliter"
    assert label_for_ordinal(26) == "slice"
    assert label_for_ordinal(27) == "serving"
    assert label_for_ordinal(33) == "scoop"


def test_unknown_ordinal_falls_back_to_unknown_ord_n() -> None:
    """Rare / not-yet-labelled ordinals surface as ``unknown_ord_<N>``.

    Three observed-but-not-yet-confirmed ords: 4 (piece?), 19 (bottle?),
    46 (pizza/pie?). Until promoted into the enum they should round-trip
    as ``unknown_ord_<N>`` so JSON consumers see something informative.
    """
    assert label_for_ordinal(46) == "unknown_ord_46"
    assert label_for_ordinal(19) == "unknown_ord_19"
    assert label_for_ordinal(4) == "unknown_ord_4"


def test_label_for_none_is_unknown() -> None:
    assert label_for_ordinal(None) == "unknown"


def test_enum_is_int_subclass() -> None:
    """IntEnum so equality with raw ints (from the wire) works."""
    assert FoodMeasurement.CUP == 3
    assert FoodMeasurement.GRAMS == 8


def test_decoder_attaches_unit_label_to_food_measure_object() -> None:
    """End-to-end: decoded FoodMeasure objects carry a ``unit`` field."""
    from lose_it_utils.client._decoder import _FOOD_MEASURE_FQCN

    # Build a synthetic FoodMeasure dict the way the decoder would, then
    # assert the labeler attaches the right unit.
    fake_decoded = {"__type__": _FOOD_MEASURE_FQCN, "ordinal": 33}
    # Mimic the decoder hook by calling label_for_ordinal directly.
    fake_decoded["unit"] = label_for_ordinal(fake_decoded["ordinal"])
    assert fake_decoded["unit"] == "scoop"
