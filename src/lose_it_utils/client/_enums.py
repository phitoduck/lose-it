"""Stable wire ordinals for Lose It! Java enums.

Single source of truth for human-readable labels of integer ordinals
that flow across the GWT wire. The decoder consults these to attach a
plain-English ``unit`` field next to the raw ``ordinal`` on decoded
objects, so ``lose-it -o json`` output is self-documenting.

These ordinals are stable across Lose It! releases (they're Java enum
values on the server). Only add entries here once a value has been
observed across multiple foods — speculative entries make the JSON
output lie.
"""

from __future__ import annotations

from enum import IntEnum


class FoodMeasurement(IntEnum):
    """``FoodMeasure.ordinal`` values — the unit a food's portion is stored in.

    Confirmed via probing >30 distinct Lose It! foods. Each value is
    matched against multiple foods and a sanity-check on the food's
    name / per-serving quantity to avoid mis-labeling.
    """

    TABLESPOON = 2
    CUP = 3
    EACH = 5
    GRAMS = 8
    FLUID_OUNCE = 10
    MILLILITER = 11
    SLICE = 26
    SERVING = 27
    SCOOP = 33

    # Observed but not yet confirmed across enough foods to label:
    #   4   — possibly "piece" (small candies)
    #   19  — possibly "bottle" / "container"
    #   46  — possibly "pizza" / "pie" / "whole-item-divided"
    # When seen on the wire these surface as ``unit="unknown_ord_<N>"``
    # in JSON output; promote them here once their meaning is confirmed.


def label_for_ordinal(ordinal: int | None) -> str:
    """Return the lowercase enum name for ``ordinal`` or ``unknown_ord_<N>``."""
    if ordinal is None:
        return "unknown"
    try:
        return FoodMeasurement(int(ordinal)).name.lower()
    except (ValueError, TypeError):
        return f"unknown_ord_{ordinal}"
