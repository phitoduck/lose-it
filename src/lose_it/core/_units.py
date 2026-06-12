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

# (native_ord, chosen_ord) → factor such that
#   quantity_in_chosen_unit = quantity_in_native_unit × factor
#
# These are unit-only constants (e.g. 1 cup = 236.588 mL). The food's
# per-serving quantity in its native unit comes from the food's own
# ``FoodServingSize.f4 / f3`` and is multiplied in by the caller.
# Same-class (volume↔volume, count↔count) cross-converts; mixed-class
# (e.g. cup→g) is intentionally absent — without per-food density data
# the answer is meaningless.
CONVERSIONS: dict[tuple[int, int], float] = {
    # cup (3) ↔ volumetric. 1 US cup = 236.5882365 mL = 8 fl oz = 16 tbsp = 48 tsp.
    (3, 3): 1.0,
    (3, 11): 236.5882365,
    (3, 10): 8.0,
    (3, 2): 16.0,
    (3, 1): 48.0,
    # fl oz (10) ↔ volumetric. 1 US fl oz = 29.5735296875 mL = 2 tbsp = 6 tsp.
    (10, 10): 1.0,
    (10, 11): 29.5735296875,
    (10, 2): 2.0,
    (10, 3): 0.125,
    (10, 1): 6.0,
    # tbsp (2) ↔ volumetric. 1 tbsp = 3 tsp.
    (2, 2): 1.0,
    (2, 11): 14.78676478125,
    (2, 10): 0.5,
    (2, 3): 0.0625,
    (2, 1): 3.0,
    # tsp (1) ↔ volumetric. 1 US teaspoon = 4.92892159375 mL.
    (1, 1): 1.0,
    (1, 11): 4.92892159375,
    (1, 10): 1.0 / 6.0,
    (1, 2): 1.0 / 3.0,
    (1, 3): 1.0 / 48.0,
    # mL (11) ↔ volumetric (the inverses).
    (11, 11): 1.0,
    (11, 3): 1.0 / 236.5882365,
    (11, 10): 1.0 / 29.5735296875,
    (11, 2): 1.0 / 14.78676478125,
    (11, 1): 1.0 / 4.92892159375,
    # Discrete units: identity only. Conversions between these (e.g. cup→each
    # or g→scoop) need per-food data, which we get from FoodServingSize.f4/f3
    # or the per-food cross-class fields (per_serving_g / per_serving_ml).
    (8, 8): 1.0,
    (5, 5): 1.0,
    (26, 26): 1.0,
    (27, 27): 1.0,
    (33, 33): 1.0,
    (4, 4): 1.0,  # piece
    (19, 19): 1.0,  # bottle
    (21, 21): 1.0,  # can
    (45, 45): 1.0,  # container (single-serve)
    (46, 46): 1.0,  # pie
}

# Case-insensitive aliases that map a user-supplied ``--serving-unit``
# value to its FoodMeasurement ordinal. Keep the keys lowercase; the
# resolver normalises the input before lookup.
UNIT_ALIASES: dict[str, int] = {
    "tsp": 1,
    "teaspoon": 1,
    "teaspoons": 1,
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
    "each": 5,
    "ea": 5,
    "piece": 4,
    "pieces": 4,
    "slice": 26,
    "slices": 26,
    "serving": 27,
    "servings": 27,
    "scoop": 33,
    "scoops": 33,
    "bottle": 19,
    "bottles": 19,
    "can": 21,
    "cans": 21,
    "container": 45,
    "containers": 45,
    "pie": 46,
    "pies": 46,
    # Deliberately omitted: bare "oz". In cooking it can mean weight ounce
    # (~28.35 g) or fluid ounce (~29.57 mL). The CLI requires the user to
    # spell out "fl_oz" for volume or "g" for weight.
}

# Canonical human-readable names for FoodMeasurement ordinals we display
# to the user. Used in dry-run output and error messages where we want a
# stable, lowercase form rather than the verbose ``measure_name`` output.
CANONICAL_UNIT_NAMES: dict[int, str] = {
    1: "tsp",
    2: "tbsp",
    3: "cup",
    4: "piece",
    5: "each",
    8: "g",
    10: "fl_oz",
    11: "mL",
    19: "bottle",
    21: "can",
    26: "slice",
    27: "serving",
    33: "scoop",
    45: "container",
    46: "pie",
}


def known_unit_names() -> list[str]:
    """Sorted list of the canonical user-facing unit names.

    Used by the ``log`` command's ``--help`` to enumerate values the user
    can pass to ``--serving-unit`` without knowing the Lose It! API's
    internal FoodMeasurement ordinals.
    """
    # Use CANONICAL_UNIT_NAMES (one name per ordinal) instead of the full
    # alias set (which has 4× the entries with synonyms like ``cups``,
    # ``c``, ``ml``, ``milliliter``). Keep sort stable by ordinal so the
    # output is predictable.
    return [CANONICAL_UNIT_NAMES[ord_] for ord_ in sorted(CANONICAL_UNIT_NAMES)]


def aliases_by_canonical() -> dict[str, list[str]]:
    """``{canonical_name: [alias, alias, ...]}`` for each known unit.

    Built by inverting :data:`UNIT_ALIASES` and grouping by ordinal.
    Aliases are sorted; the canonical name itself is excluded from the
    alias list so the ``--help`` output looks like
    ``"cup (cups, c)"`` rather than ``"cup (cup, cups, c)"``.
    """
    by_ord: dict[int, list[str]] = {}
    for alias, ord_ in UNIT_ALIASES.items():
        by_ord.setdefault(ord_, []).append(alias)
    result: dict[str, list[str]] = {}
    for ord_ in sorted(by_ord):
        canonical = CANONICAL_UNIT_NAMES.get(ord_)
        if canonical is None:
            continue
        aliases = sorted(a for a in by_ord[ord_] if a.lower() != canonical.lower())
        result[canonical] = aliases
    return result


def format_known_units_for_help() -> str:
    """Render the known units + their aliases as a human-readable string.

    Output looks like:

        tbsp (tablespoon, tablespoons, t), cup (cups, c),
        each (ea), g (gram, grams), fl_oz (floz, fl-oz, fluid_oz),
        mL (ml, milliliter, milliliters), slice (slices),
        serving (servings), scoop (scoops)

    Built from the source-of-truth tables in this module so adding a new
    FoodMeasurement ordinal + alias automatically updates the ``log``
    command's ``--help`` output.
    """
    parts: list[str] = []
    for canonical, aliases in aliases_by_canonical().items():
        if aliases:
            parts.append(f"{canonical} ({', '.join(aliases)})")
        else:
            parts.append(canonical)
    return ", ".join(parts)


def resolve_unit(raw: str) -> int:
    """Resolve a user-supplied ``--serving-unit`` value to its ordinal.

    Accepts:

    - A known unit name (case-insensitive): ``cup``, ``mL``, ``g``,
      ``fl_oz``, ``tbsp``, ``each``, ``slice``, ``serving``, ``scoop``,
      plus common aliases (``cups``, ``ml``, ``grams``, ``tablespoon``).
    - A raw integer FoodMeasurement ordinal as a string (e.g. ``"46"``
      for the ``PIE`` unit). This is an *escape hatch* for units we
      haven't yet labelled in :class:`FoodMeasurement`. Requires knowing
      the Lose It! API's internal enum values — only use it when the
      string form rejects the unit you want.

    Raises ``ValueError`` for unknown unit names and for the ambiguous
    bare ``"oz"`` (weight vs fluid).
    """
    key = raw.strip().lower().replace(" ", "_")
    if key == "oz":
        raise ValueError(
            "Bare 'oz' is ambiguous (weight vs fluid). Use 'fl_oz' for volume or 'g' for weight."
        )
    # Integer escape hatch: ``--serving-unit 46`` resolves to FoodMeasurement
    # ordinal 46. We deliberately keep this lenient (no upper bound check) —
    # the server rejects nonsense ords, and a misconfigured override is no
    # worse than the legacy --measure-ord override we used to have.
    if key.lstrip("-").isdigit():
        return int(key)
    if key not in UNIT_ALIASES:
        known = ", ".join(known_unit_names())
        raise ValueError(
            f"Unknown --serving-unit {raw!r}. Known values: {known}. "
            f"For unlisted units, pass the raw FoodMeasurement ordinal "
            f"as an integer (e.g. '--serving-unit 46' for PIE)."
        )
    return UNIT_ALIASES[key]


def conversion_factor(canonical_ord: int, chosen_ord: int) -> float | None:
    """Return the factor such that ``chosen_qty = canonical_qty × factor``.

    Returns ``None`` when the food's native unit doesn't support a
    conversion to the requested unit (e.g. cup→grams isn't physical
    without density info, so it's deliberately absent from the table).
    """
    return CONVERSIONS.get((canonical_ord, chosen_ord))
