"""Conformance tests for the unit-override flow in ``entries._build_log_payload``.

Covers the Phase 1 cases from ``docs/serving-unit-spec.md``:

| Case                                                            | Expected                                                                 |
|-----------------------------------------------------------------|--------------------------------------------------------------------------|
| ``_build_log_payload(soup, override mL@490, factor=236.588…)``  | FoodServingSize matches ``27|<canonical>|1|28|11|1|236.5882365|490|``    |
| Nutrient HashMap (per-serving)                                  | values are the food's per-serving values, NO ``× servings``              |
| Legacy ``_build_log_payload(soup, --servings=2.07)``            | Wire bytes unchanged from existing behaviour (regression guard)          |

The soup is the canonical "cups" fixture used throughout the catalog:
Organic Tomato And Roasted Red Pepper Soup, Trader Joe's, native ord 3
(cup), per-serving cal=100. The wire-evidence in the spec was captured
from the official UI logging **490 mL** of this exact food.
"""

from __future__ import annotations

import math

from lose_it_utils.client import entries
from lose_it_utils.client._config import Config
from lose_it_utils.client._models import UnsavedFoodLogEntry


def _soup_unsaved() -> UnsavedFoodLogEntry:
    """Build a soup-like UnsavedFoodLogEntry fixture.

    Mirrors the food the spec was captured against: cup-measured
    (ord=3), with the per-serving nutrient HashMap the server stores
    (cal=100, plus the supporting macros from the spec's wire snippet).
    """
    return UnsavedFoodLogEntry(
        name="Organic Tomato And Roasted Red Pepper Soup",
        brand="Trader Joe's",
        category="Soup",
        food_pk_bytes=[1] * 16,
        day_key="Z6mB_lo",
        nutrients={
            0: 100.0,  # calories
            2: 2.0,  # fat
            3: 3.5,  # saturated fat
            8: 10.0,  # cholesterol
            9: 140.0,  # sodium
            10: 15.0,  # carbs
            11: 2.0,  # fiber
            12: 5.0,  # sugar
            13: 2.0,  # protein
        },
        serving_qty=1.0,
        food_measure_ordinal=3,  # cup
    )


def _test_config() -> Config:
    """Match the placeholders the captured fixtures use."""
    return Config(
        user_id="12345678",
        user_name="test.user",
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )


# ── Override-mode byte-compare ──────────────────────────────────────────────


def test_override_food_serving_size_block_matches_wire_evidence() -> None:
    """The FoodServingSize block in override mode matches the UI's payload.

    Wire-evidence reference (from ``docs/serving-unit-spec.md``)::

        27|2.0711109608264158|1|28|11|1.0000000000000002|236.58800000000002|490|

    Two float-precision footnotes the spec explicitly tolerates:

    - The UI's captured ``f4`` is ``236.588…`` (3-decimal-place truncated
      copy of the universal cup→mL constant). The CLI sends the full
      ``236.5882365`` constant from :data:`_units.CONVERSIONS`, which is
      MORE precise. ``canonical_servings`` reflects this:
      ``490 / 236.5882365 ≈ 2.07110889…`` rather than the captured
      ``2.07111096…``. Both round to the same displayed value in the
      official UI.
    - ``fmt_num`` collapses integer-valued floats to plain ints
      (``1.0`` → ``"1"``), matching the existing legacy wire shape.

    The resulting wire block is::

        27|2.0711088904878836|1|28|11|1|236.5882365|490|
    """
    config = _test_config()
    soup = _soup_unsaved()
    factor = 236.5882365  # universal US-cup → mL constant
    qty_in_mL = 490.0
    canonical_servings = qty_in_mL / factor

    body = entries._build_log_payload(
        config,
        soup,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=canonical_servings,
        measure_ord_override=11,
        quantity_in_chosen_unit=qty_in_mL,
        conversion_factor=factor,
    )

    # FoodServingSize block: 27|<canonical>|1|28|<chosen_ord>|1|<factor>|<qty>|
    assert "|27|2.0711088904878836|1|28|11|1|236.5882365|490|" in body, (
        f"FoodServingSize block missing or wrong; got body tail "
        f"{body[body.find('|27|') - 20 : body.find('|27|') + 80]!r}"
    )


def test_override_nutrient_hashmap_is_per_serving_not_scaled() -> None:
    """The FoodNutrients HashMap on the wire is per-serving, not scaled.

    The server multiplies these by ``FoodServing.quantity`` to display
    totals. Pre-scaling them here was the bug fixed in PR #22; this is a
    regression guard against re-introducing it in the override path.
    """
    config = _test_config()
    soup = _soup_unsaved()
    factor = 236.5882365
    qty_in_mL = 490.0
    canonical_servings = qty_in_mL / factor

    body = entries._build_log_payload(
        config,
        soup,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=canonical_servings,
        measure_ord_override=11,
        quantity_in_chosen_unit=qty_in_mL,
        conversion_factor=factor,
    )

    # Calories per serving = 100. If the bug were present we'd see
    # 207 (= 100 × 2.07) instead.
    # Wire layout: "25|0|26|100|" → "FoodMeasurement ref|ord=0|Double ref|100"
    assert "|25|0|26|100|" in body, (
        "calorie HashMap entry should be per-serving (100), not pre-scaled "
        "(would be 207 for the soup at 490 mL)"
    )
    # Sodium per serving = 140. Bug case would be 290.
    assert "|25|9|26|140|" in body


def test_override_food_serving_quantity_is_canonical_servings() -> None:
    """The FoodServing.quantity slot (= ``servings`` passed in) is canonical."""
    config = _test_config()
    soup = _soup_unsaved()
    factor = 236.5882365
    qty_in_mL = 490.0
    canonical_servings = qty_in_mL / factor

    body = entries._build_log_payload(
        config,
        soup,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=canonical_servings,
        measure_ord_override=11,
        quantity_in_chosen_unit=qty_in_mL,
        conversion_factor=factor,
    )
    # "|1|<canonical_servings>|24|" — the FoodServing wire shape (see
    # entries.py:152-155) is "1|<servings>|24|<n_nutrients>|".
    # See test_override_food_serving_size_block_matches_wire_evidence for
    # why the canonical-servings repr is ``2.0711088904878836`` (we use
    # the full-precision US-cup constant, not the UI's 3-decimal copy).
    assert "|1|2.0711088904878836|24|" in body, (
        "FoodServing.quantity should be the canonical-servings value"
    )


def test_override_uses_chosen_unit_ord_not_native() -> None:
    """The FoodMeasure ord on the wire is the *chosen* ord, not the native."""
    config = _test_config()
    soup = _soup_unsaved()  # native ord = 3 (cup)
    factor = 236.5882365
    qty_in_mL = 490.0

    body = entries._build_log_payload(
        config,
        soup,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=qty_in_mL / factor,
        measure_ord_override=11,  # mL
        quantity_in_chosen_unit=qty_in_mL,
        conversion_factor=factor,
    )
    # FoodMeasure ref slot is 11 (mL), not 3 (cup).
    assert "|28|11|" in body
    # And the native ord (3) should NOT appear anywhere in the
    # FoodServingSize block — guards against a partial-override bug.
    after = body[body.find("|27|") :]
    assert "|28|3|" not in after, "FoodMeasure ref should be the chosen ord"


# ── Legacy regression guard ─────────────────────────────────────────────────


def test_legacy_no_override_unchanged_for_soup() -> None:
    """With no override params, the wire shape matches pre-spec behavior."""
    config = _test_config()
    soup = _soup_unsaved()  # cup-measured

    body = entries._build_log_payload(
        config,
        soup,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=2.07,
    )
    # Legacy FoodServingSize for cup-measured food: |27|2.07|1|28|3|1|1|2.07|
    # The portion_size slot equals servings (cup is not the gram special case).
    assert "|27|2.07|1|28|3|1|1|2.07|" in body, body[body.find("|27|") : body.find("|27|") + 40]


def test_legacy_grams_path_still_gram_special_case() -> None:
    """Legacy ``--grams`` path: when caller passes servings=1.2 on ord=8
    food (no override), portion_size should be 120 (= 1.2 × 100)."""
    config = _test_config()
    chicken = UnsavedFoodLogEntry(
        name="Chicken Strips",
        brand="Real Good Foods",
        category="Chicken",
        food_pk_bytes=[1] * 16,
        day_key="Z6mB_lo",
        nutrients={0: 130.0},
        serving_qty=1.0,
        food_measure_ordinal=8,  # grams
    )

    body = entries._build_log_payload(
        config,
        chicken,
        meal_ordinal=3,
        day_key="Z6mB_lo",
        day_num=9290,
        servings=1.2,
    )
    # FoodServingSize: |27|120|1|28|8|1|1|120|
    assert "|27|120|1|28|8|1|1|120|" in body


# ── Defensive checks on the API ─────────────────────────────────────────────


def test_override_requires_all_three_params() -> None:
    """Passing just one of the override params raises ``ValueError``."""
    import pytest

    config = _test_config()
    soup = _soup_unsaved()
    with pytest.raises(ValueError, match="must all be set together"):
        entries._build_log_payload(
            config,
            soup,
            meal_ordinal=3,
            day_key="Z6mB_lo",
            day_num=9290,
            servings=2.07,
            measure_ord_override=11,  # only one set
        )


def test_canonical_servings_math_is_correct() -> None:
    """Sanity: 490 mL / 236.5882365 mL-per-cup ≈ 2.0711 cups.

    The wire-evidence captured value (2.0711109608264158) is what the UI
    computes against its 3-decimal-place ``236.588`` copy of the
    constant; we use the full ``236.5882365`` so the canonical value is
    ~2.0711088904878836 (rounds the same).
    """
    canonical = 490.0 / 236.5882365
    assert math.isclose(canonical, 2.0711088904878836, rel_tol=1e-12)
    # Both round to 2.07 cups; the UI displays both as "490 mL".
    assert math.isclose(canonical, 2.0711109608264158, abs_tol=1e-4)
