"""Unit conversion table mirroring the official UI's display-unit dropdown.

All factors are US customary measurement constants. The Lose It! web UI
uses these same constants (e.g. ``236.5882365`` mL/US cup, confirmed by
inspecting the official UI's outbound wire payload); the figures here
are not food-specific, only unit-specific.

This module deliberately does NOT parse a combined ``"490mL"`` string.
The CLI exposes ``--serving-amount`` (float) and ``--serving-unit``
(str) as two separate options, and :func:`resolve_unit` only handles
the unit-name → ordinal lookup. The two-flag form gives us familiar
``<flag> <value>`` ergonomics, tab-completable unit values, and
flag-validation errors instead of silent regex failures.
"""

from __future__ import annotations

# (canonical_ord, chosen_ord) → factor such that
#   quantity_in_chosen_unit = canonical_servings × factor
# A diagonal entry (ord, ord) is always 1.0 — included so callers can do
# the lookup unconditionally.
CONVERSIONS: dict[tuple[int, int], float] = {
    # cup (3) ↔ volumetric units. 1 US cup = 236.5882365 mL = 8 fl oz = 16 tbsp.
    (3, 3): 1.0,
    (3, 11): 236.5882365,
    (3, 10): 8.0,
    (3, 2): 16.0,
    # fl oz (10) ↔ volumetric. 1 US fl oz = 29.5735296875 mL = 2 tbsp.
    (10, 10): 1.0,
    (10, 11): 29.5735296875,
    (10, 2): 2.0,
    (10, 3): 0.125,
    # tbsp (2) ↔ volumetric.
    (2, 2): 1.0,
    (2, 11): 14.78676478125,
    (2, 10): 0.5,
    (2, 3): 0.0625,
    # mL (11) ↔ volumetric (the inverses).
    (11, 11): 1.0,
    (11, 3): 1.0 / 236.5882365,
    (11, 10): 1.0 / 29.5735296875,
    (11, 2): 1.0 / 14.78676478125,
    # grams (8) — kept as-is to mirror the existing `--grams` flag's
    # convention that "1 serving = 100 g". This is a *special* case in
    # the existing CLI (entries.py:103-105); we preserve it intentionally.
    (8, 8): 100.0,
}

# Case-insensitive aliases that map a user-supplied ``--serving-unit``
# value to its FoodMeasurement ordinal. Keep the keys lowercase; the
# resolver normalises the input before lookup.
UNIT_ALIASES: dict[str, int] = {
    "cup": 3,
    "cups": 3,
    "c": 3,
    "ml": 11,
    "milliliter": 11,
    "milliliters": 11,
    "fl_oz": 10,
    "floz": 10,
    "fl-oz": 10,
    "fluid_oz": 10,
    "tbsp": 2,
    "tablespoon": 2,
    "tablespoons": 2,
    "t": 2,
    "g": 8,
    "gram": 8,
    "grams": 8,
    # Deliberately omitted: bare "oz". In cooking it can mean weight ounce
    # (~28.35 g) or fluid ounce (~29.57 mL). The CLI requires the user to
    # spell out "fl_oz" for volume or "g" for weight.
}

# Canonical human-readable names for FoodMeasurement ordinals we display
# to the user. Used in dry-run output and error messages where we want a
# stable, lowercase form rather than the verbose ``measure_name`` output.
CANONICAL_UNIT_NAMES: dict[int, str] = {
    3: "cup",
    11: "mL",
    10: "fl_oz",
    2: "tbsp",
    8: "g",
}


def resolve_unit(raw: str) -> int:
    """Resolve a user-supplied ``--serving-unit`` value to its ordinal.

    Raises ``ValueError`` for unknown units and the ambiguous bare
    ``"oz"`` (which can mean weight ounce or fluid ounce depending on
    context).
    """
    key = raw.strip().lower().replace(" ", "_")
    if key == "oz":
        raise ValueError(
            "Bare 'oz' is ambiguous (weight vs fluid). Use 'fl_oz' for volume or 'g' for weight."
        )
    if key not in UNIT_ALIASES:
        raise ValueError(
            f"Unknown --serving-unit {raw!r}. Known values: "
            + ", ".join(sorted(set(UNIT_ALIASES.keys())))
        )
    return UNIT_ALIASES[key]


def conversion_factor(canonical_ord: int, chosen_ord: int) -> float | None:
    """Return the factor such that ``chosen_qty = canonical_qty × factor``.

    Returns ``None`` when the food's native unit doesn't support a
    conversion to the requested unit (e.g. cup→grams isn't physical
    without density info, so it's deliberately absent from the table).
    """
    return CONVERSIONS.get((canonical_ord, chosen_ord))
