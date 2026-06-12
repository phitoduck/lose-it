"""Stable wire ordinals for Lose It! Java enums.

Single source of truth for human-readable labels of integer ordinals
that flow across the GWT wire. Two enums share this module because
they share the wire type ``FoodMeasurement`` (the Java class), but they
carry semantically distinct values: one identifies a unit of measure
for a food's portion, the other identifies a nutrient slot in the
food's nutrient HashMap.

These ordinals are stable across Lose It! releases (they're Java enum
values on the server). Only add entries once a value has been observed
across multiple foods + cross-referenced against the official UI's
labeled display — speculative entries make the JSON output lie.
"""

from __future__ import annotations

from enum import IntEnum


class FoodMeasurement(IntEnum):
    """``FoodMeasure.ordinal`` values — the *unit* a food's portion is stored in.

    Confirmed via probing >50 distinct Lose It! foods. Each value is
    matched against multiple foods and a sanity-check on the food's
    name / per-serving quantity to avoid mis-labeling.

    Note: the Java class for this enum is ``FoodMeasurement`` (from
    ``healthdata.model.shared.food``), the same class used as the key
    type in the FoodNutrients HashMap. The semantics differ by context —
    on a ``FoodMeasure`` it's a unit; in a HashMap key it's a nutrient
    slot (see :class:`FoodNutrient`).
    """

    TEASPOON = 1  # confirmed: Raw Honey "1 Teaspoon" per_serving_ml=4.92892 (= exactly 1 US tsp)
    TABLESPOON = 2
    CUP = 3
    PIECE = 4  # confirmed: Reese's PB Eggs (f4=4, f5=3 = 3 candies), gum sticks
    EACH = 5
    GRAMS = 8
    FLUID_OUNCE = 10
    MILLILITER = 11
    BOTTLE = 19  # confirmed: Slimfast shake bottle, Diet Coke can (f0=1.666 = 12oz/7.2oz)
    CAN = 21  # confirmed: Pepsi/Mountain Dew/Dr Pepper per_serving_ml=355 (= 12 fl oz US can)
    SLICE = 26
    SERVING = 27
    SCOOP = 33
    CONTAINER = 45  # confirmed: Chobani/Fage "Indiv. Container" / "Single Serve" yogurts
    PIE = 46  # confirmed: Milton's cauliflower pizza (f4=0.25 = 1/4 pie per serving)

    # Observed but per-food semantics inconsistent — needs more probing
    # before labeling. Each surfaces as ``unit="unknown_ord_<N>"`` in JSON
    # output so consumers can still see something informative:
    #   6   — appears on cooked meat / steak / pasta. Per-food g/unit ratio
    #         varies (44g for chicken skinless cooked, 59g for grilled,
    #         100g for steak qty=1) — likely a per-food display unit, not
    #         a stable measurement constant.
    #   16  — observed on Orgain protein shake (RTD).
    #   34  — only one sample (Quaker Old Fashioned Oats Dry, 40g per
    #         "1 unit" = 150 cal = standard ½-cup dry). Could be "½ cup"
    #         but a single sample isn't enough.
    #   35  — inconsistent: SkinnyPop 1 unit = 28g (= 1 oz, looks like
    #         "ounce") but Bush's kale 1 unit = ~115g (not an ounce).
    #         Likely "package" / "bag" / "ounce" depending on the food.


class FoodNutrient(IntEnum):
    """Nutrient ordinals — keys in the food's FoodNutrients HashMap.

    Cross-referenced against the official Lose It! UI's labeled nutrition
    panel for known foods (Trader Joe's tomato soup, Realgood Foods
    chicken strips, Built Bar puff, Orgain protein, etc.). The wire's
    HashMap key type is the same Java class (``FoodMeasurement``) as
    the unit enum above, but the values are semantically different.

    Per-serving values (the food's "1 serving" definition). The CLI's
    log path scales these by ``canonical_servings`` to compute totals.

    Confirmed via UI scrape + bulk wire probe of 53 foods. See
    ``~/lose-it-evidence/2026-06-12-mapping-synthesis.md`` for the
    cross-reference table.
    """

    CALORIES = 0
    SERVING_VOLUME_ML = 1  # present only for volume-stored foods (cup/fl_oz/mL)
    SERVING_WEIGHT_G = 2  # present only for mass-stored foods (grams)
    TOTAL_FAT_G = 3
    SATURATED_FAT_G = 4
    CHOLESTEROL_MG = 8
    SODIUM_MG = 9
    CARB_G = 10
    FIBER_G = 11
    SUGAR_G = 12
    PROTEIN_G = 13

    # Observed but not yet confirmed:
    #   5, 6, 7         — varies, rare; possibly micronutrients
    #   14-29           — micronutrients (calcium, iron, potassium, etc.)
    #     ord=18 hits "30" for chicken (≈ cholesterol?), "81" for Built Bar
    #     ord=19 hits "0.9" chicken, "3.0" Built Bar, "6.4" Orgain — possibly IRON_MG
    #     ord=22 hits "300" chicken, "188" Built Bar, "120" Orgain — possibly POTASSIUM_MG
    # Unmapped slots surface as ``"unknown_nutrient_<N>"`` in JSON output.


def label_for_ordinal(ordinal: int | None) -> str:
    """Return the lowercase :class:`FoodMeasurement` enum name for ``ordinal``.

    Used by the decoder to attach a human-readable ``unit`` label next to
    ``ordinal`` on every decoded ``FoodMeasure`` object. Falls back to
    ``unknown_ord_<N>`` for unmapped values so JSON consumers see
    something informative rather than a bare integer.
    """
    if ordinal is None:
        return "unknown"
    try:
        return FoodMeasurement(int(ordinal)).name.lower()
    except (ValueError, TypeError):
        return f"unknown_ord_{ordinal}"


def label_for_nutrient(ordinal: int | None) -> str:
    """Return the lowercase :class:`FoodNutrient` enum name for ``ordinal``.

    Used by the food-parser to convert the raw HashMap (``{ord: value}``)
    into a labeled dict (``{"calories": 100, "sodium_mg": 140, ...}``).
    Unmapped values fall back to ``unknown_nutrient_<N>``.
    """
    if ordinal is None:
        return "unknown_nutrient"
    try:
        return FoodNutrient(int(ordinal)).name.lower()
    except (ValueError, TypeError):
        return f"unknown_nutrient_{ordinal}"
