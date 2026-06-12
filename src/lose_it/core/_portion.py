"""Pure portion-size resolution for the ``log`` flow.

The ``log`` command exposes two mutually-exclusive ways to describe how
much of a food was consumed:

1. ``servings`` — a raw canonical multiplier (the server multiplies the
   food's per-serving nutrients by this number).
2. ``serving_amount`` + ``serving_unit`` — a quantity in a chosen unit
   (e.g. ``490 mL``, ``61 g``, ``2 slices``).

The second form is much more ergonomic but requires converting the
chosen unit back into a canonical servings count, plus generating the
wire-override block (``measure_ord_override`` / ``conversion_factor`` /
``quantity_in_chosen_unit``) the server expects.

This module is the pure brain of that conversion: it takes an
:class:`UnsavedFoodLogEntry` and the user's portion-size knobs, and
returns a :class:`ResolvedPortion` with everything the wire builder + the
display formatter need. It never touches HTTP. Errors are raised as
:class:`PortionError` with a short ``code`` so the CLI can map them to
machine-readable JSON output.

Lifted from ``cli.log`` (~lines 596-770 of the old ``cli.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..enums import ServingUnit
from ..models import UnsavedFoodLogEntry
from ._config import measure_name
from ._units import CANONICAL_UNIT_NAMES, conversion_factor, resolve_unit

# FoodMeasurement ordinals used for cross-class lookups.
_GRAMS = 8
_ML = 11
_VOLUME_ORDS = {2, 3, 10, 11}  # tbsp, cup, fl_oz, mL — destination units for mL→X
_NUTRIENT_CALORIES = 0

__all__ = [
    "PortionError",
    "ResolvedPortion",
    "resolve_portion",
    "scaled_calories",
    "validate_portion_args",
]


# Public error codes — kept stable for JSON output. New conditions get a
# new code rather than overloading an existing one.
ERR_SERVING_PAIR_INCOMPLETE = "serving_pair_incomplete"
ERR_MUTUALLY_EXCLUSIVE_FLAGS = "mutually_exclusive_flags"
ERR_NON_POSITIVE_SERVING_AMOUNT = "non_positive_serving_amount"
ERR_UNKNOWN_SERVING_UNIT = "unknown_serving_unit"
ERR_UNIT_NOT_SUPPORTED = "unit_not_supported"


class PortionError(ValueError):
    """Validation/resolution failure with a stable machine-readable ``code``.

    Attributes:
        code: One of the ``ERR_*`` constants above.
        context: Extra fields for structured output (e.g. ``native_unit``,
            ``requested_unit`` for ``unit_not_supported``). Stays empty
            for simple validation errors.
    """

    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


@dataclass(frozen=True)
class ResolvedPortion:
    """Wire + display values produced by :func:`resolve_portion`.

    ``canonical_servings`` is what the server multiplies the food's
    per-serving nutrients by — always set.

    The next three fields are non-``None`` only when the user passed
    ``serving_amount``/``serving_unit`` (the override path). When ``None``
    the wire builder emits the legacy FoodServingSize block and skips the
    chosen-unit override.

    ``display_*`` are pre-rendered values the CLI uses for the
    "✅ Logged …" / "🟡 DRY RUN — would log …" line; downstream code
    should not have to re-derive them.
    """

    canonical_servings: float
    measure_ord_override: int | None
    quantity_in_chosen_unit: float | None
    conversion_factor: float | None
    display_amount: float
    display_unit: str


def validate_portion_args(
    servings: float,
    serving_amount: float | None,
    serving_unit: ServingUnit | str | None,
) -> int | None:
    """Cheap arg-only validation of the portion knobs.

    Doesn't need an :class:`UnsavedFoodLogEntry` — exists so callers can
    fail fast on bad input before any HTTP work happens. Returns:

    - ``None`` for the legacy ``--servings`` path (no override).
    - The chosen FoodMeasurement ordinal for the unit-based path.

    Raises :class:`PortionError` with codes
    ``serving_pair_incomplete`` / ``mutually_exclusive_flags`` /
    ``non_positive_serving_amount`` / ``unknown_serving_unit``.
    The cross-unit support check (``unit_not_supported``) is per-food
    and still lives in :func:`resolve_portion`.
    """
    sa_set = serving_amount is not None
    su_set = serving_unit is not None

    if not sa_set and not su_set:
        return None
    if sa_set != su_set:
        raise PortionError(
            ERR_SERVING_PAIR_INCOMPLETE,
            "--serving-amount and --serving-unit must be passed together "
            "(neither is meaningful alone).",
        )
    if servings != 1.0:
        raise PortionError(
            ERR_MUTUALLY_EXCLUSIVE_FLAGS,
            "--serving-amount / --serving-unit are mutually exclusive with --servings.",
        )
    assert serving_amount is not None and serving_unit is not None  # narrow for mypy
    if serving_amount <= 0:
        raise PortionError(
            ERR_NON_POSITIVE_SERVING_AMOUNT,
            f"--serving-amount must be positive (got {serving_amount}).",
        )
    unit_str = serving_unit.value if isinstance(serving_unit, ServingUnit) else serving_unit
    try:
        return resolve_unit(unit_str)
    except ValueError as exc:
        raise PortionError(ERR_UNKNOWN_SERVING_UNIT, str(exc)) from exc


def resolve_portion(
    unsaved: UnsavedFoodLogEntry,
    servings: float = 1.0,
    serving_amount: float | None = None,
    serving_unit: ServingUnit | str | None = None,
) -> ResolvedPortion:
    """Validate the portion knobs and resolve them to wire + display values.

    Decision tree:

    1. If both ``serving_amount`` and ``serving_unit`` are ``None``,
       this is the legacy ``--servings N`` path; return the raw multiplier
       and render in the food's native unit.
    2. Otherwise both must be set and ``servings`` must be left at its
       default of ``1.0`` (passing both is treated as user confusion).
    3. Resolve ``serving_unit`` (string or alias) to its FoodMeasurement
       ordinal via :func:`lose_it.core._units.resolve_unit`.
    4. If the food's native unit is in the same class as the chosen unit
       (both volumetric, both same count enum), use the static
       ``CONVERSIONS`` table.
    5. Otherwise look for a per-food cross-class hint in
       ``unsaved.per_serving_g`` / ``unsaved.per_serving_ml``.
    6. If neither path produces a factor, raise ``PortionError`` with
       code ``unit_not_supported`` and a message that names both units —
       the caller can hint the user toward ``--servings`` or a different
       food entry.

    Raises:
        PortionError: any of the codes above.
    """
    chosen_ord = validate_portion_args(servings, serving_amount, serving_unit)

    # ── Step 1: legacy --servings path ──────────────────────────────────
    if chosen_ord is None:
        display_unit = unsaved.food_measure_unit or measure_name(unsaved.food_measure_ordinal)
        return ResolvedPortion(
            canonical_servings=servings,
            measure_ord_override=None,
            quantity_in_chosen_unit=None,
            conversion_factor=None,
            display_amount=servings,
            display_unit=display_unit,
        )

    assert serving_amount is not None and serving_unit is not None  # narrowed by validator
    unit_str = serving_unit.value if isinstance(serving_unit, ServingUnit) else serving_unit

    # ── Steps 4-5: compute chosen-unit qty per canonical serving ────────
    measure_ord = unsaved.food_measure_ordinal
    native_ord = measure_ord if measure_ord is not None else 0
    chosen_qty_per_serving: float | None = None

    cf_native_to_chosen = conversion_factor(native_ord, chosen_ord)
    if cf_native_to_chosen is not None:
        # Same-class conversion — use the static table.
        f3 = unsaved.canonical_per_serving or 1.0
        f4 = unsaved.native_qty_per_serving or 1.0
        native_qty_per_serving = f4 / f3 if f3 else 1.0
        chosen_qty_per_serving = native_qty_per_serving * cf_native_to_chosen
    elif chosen_ord == _GRAMS and unsaved.per_serving_g:
        # Cross-class: food's own FoodNutrients HashMap carries grams/serving.
        chosen_qty_per_serving = unsaved.per_serving_g
    elif chosen_ord in _VOLUME_ORDS and unsaved.per_serving_ml:
        # Cross-class via mL — chain through the static volume table.
        cf_ml_to_chosen = conversion_factor(_ML, chosen_ord)
        if cf_ml_to_chosen is not None:
            chosen_qty_per_serving = unsaved.per_serving_ml * cf_ml_to_chosen

    # ── Step 6: failure mode — no factor available ──────────────────────
    if chosen_qty_per_serving is None:
        chosen_name = CANONICAL_UNIT_NAMES.get(chosen_ord, unit_str)
        native_name = unsaved.food_measure_unit or measure_name(measure_ord)
        raise PortionError(
            ERR_UNIT_NOT_SUPPORTED,
            f"{unsaved.name!r} is measured in {native_name!r}; "
            f"the SDK doesn't have a {native_name}→{chosen_name} conversion "
            f"factor, and the food doesn't carry the per-serving "
            f"{chosen_name} value in its nutrients. Pick a different food "
            f"entry whose native unit is {chosen_name}, or pass servings= "
            f"to log in the food's native unit.",
            food=unsaved.name,
            native_unit=native_name,
            requested_unit=chosen_name,
        )

    canonical = serving_amount / chosen_qty_per_serving
    display_unit = CANONICAL_UNIT_NAMES.get(chosen_ord, unit_str)
    return ResolvedPortion(
        canonical_servings=canonical,
        measure_ord_override=chosen_ord,
        quantity_in_chosen_unit=serving_amount,
        conversion_factor=chosen_qty_per_serving,
        display_amount=serving_amount,
        display_unit=display_unit,
    )


def scaled_calories(
    unsaved: UnsavedFoodLogEntry, canonical_servings: float
) -> float | None:
    """Return ``cal_per_serving × canonical_servings``, or ``None`` if absent.

    The unsaved-entry response carries the food's nutrient map keyed by
    FoodNutrient ordinal — calories sit at ordinal 0. Some food
    categories (raw produce, condiments) omit calories entirely; the
    return is ``None`` so callers can choose to suppress the calorie
    suffix in display output rather than printing ``0 cal``.
    """
    per_serving = (unsaved.nutrients or {}).get(_NUTRIENT_CALORIES)
    if per_serving is None:
        return None
    return per_serving * canonical_servings
