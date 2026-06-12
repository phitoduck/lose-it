"""Regression guard for the ``HashMap`` entry-pair decoder.

Background
----------

A user-reported probe of a tomato-soup ``getUnsavedFoodLogEntry`` response
suggested that the schema-driven decoder in
:mod:`lose_it.client._decoder` was pairing ``(key_{i+1}, value_i)`` instead
of ``(key_i, value_i)`` when reading ``java.util.HashMap`` entries — i.e.
an off-by-one in the LIFO ``_MAP_TYPES`` branch.

Hand-tracing the LIFO token stream of the closest captured fixture
(``get_unsaved_tortilla.txt`` — same wire shape, ``FoodMeasurement →
Double``) against the GWT ``Map_CustomFieldSerializer`` algorithm shows
the current decoder is already aligned correctly:

::

    for (int i = 0; i < size; i++) {
        Object key = streamReader.readObject();
        Object value = streamReader.readObject();
        map.put(key, value);
    }

The key (type-ref + ordinal pair) sits *below* the value (type-ref +
double pair) on the LIFO stack within each entry, so the key pops out
first — exactly what ``_decoder._read_typed`` does. The 9 ``(ordinal,
value)`` pairs below match the decoded output byte-for-byte.

The test is kept as a permanent regression guard: any future patch that
flips the read order (e.g. swaps ``key``/``value`` in ``_MAP_TYPES``) will
trip this assertion *and* the two existing tests
``test_daily_details_parses_food_log_entries`` and
``test_get_unsaved_parses_captured_response`` that depend indirectly on
HashMap-ordering correctness.
"""

from __future__ import annotations

from lose_it.client._decoder import decode_response

# Expected ``(FoodMeasurement.ordinal, Double)`` pairs, in LIFO read order,
# for the nutrient HashMap inside the captured ``get_unsaved_tortilla.txt``
# response. Derived by hand-tracing the data section of the fixture against
# the GWT Map serializer (see module docstring).
EXPECTED_PAIRS: list[tuple[int, float]] = [
    (13, 5.0),
    (10, 4.0),
    (8, 0.0),
    (11, 15.0),
    (0, 70.0),
    (3, 3.0),
    (12, 0.0),
    (4, 1.0),
    (9, 320.0),
]


def test_unsaved_tortilla_nutrient_hashmap_pairs(fixture_text) -> None:
    """Each wire ``(key, value)`` lands at the matching slot in ``entries``.

    The decoder output must produce the exact ordered pair sequence above —
    not a shifted-by-one variant. Catches a regression where the
    ``_MAP_TYPES`` branch in :func:`_decoder._read_typed` swaps the order
    in which it pops ``key`` and ``value`` off the LIFO stream.
    """
    body = fixture_text("get_unsaved_tortilla.txt")
    decoded = decode_response(body, strict=True)

    # Walk: response.f3 (FoodLogEntry) → .f2 (FoodServing) → .f0 (FoodNutrients)
    #   → .f2 (the HashMap).
    nutrient_map = decoded["f3"]["f2"]["f0"]["f2"]
    assert nutrient_map["__type__"].startswith("java.util.HashMap"), (
        f"expected a HashMap, got {nutrient_map.get('__type__')!r}"
    )

    entries = nutrient_map["entries"]
    assert len(entries) == len(EXPECTED_PAIRS), (
        f"expected {len(EXPECTED_PAIRS)} entries on the wire, got {len(entries)}"
    )

    actual: list[tuple[int, float]] = []
    for key, value in entries:
        # Keys are decoded FoodMeasurement enum dicts. We only care about
        # the ordinal here — the unit-labeling pass is exercised elsewhere.
        assert isinstance(key, dict), f"expected enum dict key, got {key!r}"
        actual.append((int(key["ordinal"]), float(value)))

    assert actual == EXPECTED_PAIRS, (
        "HashMap entries are shifted relative to the wire — "
        "this is the off-by-one bug. "
        f"expected={EXPECTED_PAIRS} actual={actual}"
    )
