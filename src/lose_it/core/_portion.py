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
_VOLUME_ORDS = {1, 2, 3, 10, 11}  # tsp, tbsp, cup, fl_oz, mL — reachable from mL
_NUTRIENT_CALORIES = 0

__all__ = [
    "PortionError",
    "ResolvedPortion",
    "resolve_portion",
    "scaled_calories",
    "supported_units",
    "validate_portion_args",
]


# Public error codes — kept stable for JSON output. New conditions get a
# new code rather than overloading an existing one.
ERR_SERVING_PAIR_INCOMPLETE = "serving_pair_incomplete"
ERR_MUTUALLY_EXCLUSIVE_FLAGS = "mutually_exclusive_flags"
ERR_NON_POSITIVE_SERVING_AMOUNT = "non_positive_serving_amount"
ERR_UNKNOWN_SERVING_UNIT = "unknown_serving_unit"
ERR_UNIT_NOT_SUPPORTED = "unit_not_supported"
ERR_NON_POSITIVE_GRAMS_PER_ML = "non_positive_grams_per_ml"


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
    grams_per_ml: float | None = None,
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
    5b. Still nothing, and ``grams_per_ml`` (the ingredient's density) is
       given: bridge volume ↔ weight through it. Many generic entries
       (honey, cocoa powder, nut butters) store only a volume, so grams
       are otherwise unreachable — Lose It!'s own UI can't do it either.
    6. If no path produces a factor, raise ``PortionError`` with
       code ``unit_not_supported``; the message and ``context`` list the
       units the food does support (see :func:`supported_units`).

    Raises:
        PortionError: any of the codes above.
    """
    chosen_ord = validate_portion_args(servings, serving_amount, serving_unit)
    if grams_per_ml is not None and grams_per_ml <= 0:
        raise PortionError(
            ERR_NON_POSITIVE_GRAMS_PER_ML,
            f"grams_per_ml must be positive (got {grams_per_ml}).",
        )

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
    chosen_qty_per_serving = _qty_per_serving(unsaved, chosen_ord, grams_per_ml)

    # ── Step 6: failure mode — no factor available ──────────────────────
    if chosen_qty_per_serving is None:
        chosen_name = CANONICAL_UNIT_NAMES.get(chosen_ord, unit_str)
        native_name = unsaved.food_measure_unit or measure_name(unsaved.food_measure_ordinal)
        supported = supported_units(unsaved)
        # Would a density bridge the gap? Only when the food has a volume
        # (or weight) side and the request is for the other side.
        density_helps = grams_per_ml is None and (
            (chosen_ord == _GRAMS and _per_serving_ml(unsaved) is not None)
            or (chosen_ord in _VOLUME_ORDS and _per_serving_g(unsaved) is not None)
        )
        density_hint = (
            "pass grams_per_ml (CLI: --grams-per-ml) with the ingredient's "
            "density to convert between volume and weight, "
            if density_helps
            else ""
        )
        raise PortionError(
            ERR_UNIT_NOT_SUPPORTED,
            f"{unsaved.name!r} is measured in {native_name!r} and Lose It! "
            f"stores no {native_name}→{chosen_name} conversion for it. "
            f"Units this food supports: {', '.join(supported) or 'none'}. "
            f"Either {density_hint}pick a food entry that supports "
            f"{chosen_name}, or use one of the supported units.",
            food=unsaved.name,
            native_unit=native_name,
            requested_unit=chosen_name,
            supported_units=supported,
            grams_per_ml_would_help=density_helps,
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


def _native_qty_per_serving(unsaved: UnsavedFoodLogEntry) -> float:
    """The food's per-canonical-serving quantity in its native unit (``f4/f3``)."""
    f3 = unsaved.canonical_per_serving or 1.0
    f4 = unsaved.native_qty_per_serving or 1.0
    return f4 / f3 if f3 else 1.0


def _per_serving_ml(unsaved: UnsavedFoodLogEntry) -> float | None:
    """mL per canonical serving: the stored slot, else derived from a volume native unit."""
    if unsaved.per_serving_ml:
        return unsaved.per_serving_ml
    cf = conversion_factor(unsaved.food_measure_ordinal or 0, _ML)
    if cf is not None:
        return _native_qty_per_serving(unsaved) * cf
    return None


def _per_serving_g(unsaved: UnsavedFoodLogEntry) -> float | None:
    """Grams per canonical serving: the stored slot, else a gram native unit."""
    if unsaved.per_serving_g:
        return unsaved.per_serving_g
    if unsaved.food_measure_ordinal == _GRAMS:
        return _native_qty_per_serving(unsaved)
    return None


def _qty_per_serving(
    unsaved: UnsavedFoodLogEntry,
    chosen_ord: int,
    grams_per_ml: float | None = None,
) -> float | None:
    """Quantity of ``chosen_ord`` units in one canonical serving, or ``None``.

    Tries, in order: the static same-class table (tbsp→cup, g→g, …), the
    food's own per-serving gram/mL slots, and finally — only when the
    caller supplied a density — a volume ↔ weight bridge.
    """
    native_ord = unsaved.food_measure_ordinal if unsaved.food_measure_ordinal is not None else 0
    cf_native_to_chosen = conversion_factor(native_ord, chosen_ord)
    if cf_native_to_chosen is not None:
        return _native_qty_per_serving(unsaved) * cf_native_to_chosen
    if chosen_ord == _GRAMS and unsaved.per_serving_g:
        return unsaved.per_serving_g
    cf_ml_to_chosen = conversion_factor(_ML, chosen_ord) if chosen_ord in _VOLUME_ORDS else None
    if cf_ml_to_chosen is not None and unsaved.per_serving_ml:
        return unsaved.per_serving_ml * cf_ml_to_chosen
    if grams_per_ml:
        if chosen_ord == _GRAMS:
            ml = _per_serving_ml(unsaved)
            if ml is not None:
                return ml * grams_per_ml
        elif cf_ml_to_chosen is not None:
            g = _per_serving_g(unsaved)
            if g is not None:
                return g / grams_per_ml * cf_ml_to_chosen
    return None


def supported_units(unsaved: UnsavedFoodLogEntry) -> list[str]:
    """Unit names ``resolve_portion`` accepts for this food without a density.

    Always includes the food's native unit; adds whatever the static
    same-class table and the food's per-serving gram/mL slots unlock.
    Ordered by FoodMeasurement ordinal, like :func:`known_unit_names`.
    """
    return [
        name
        for ord_, name in sorted(CANONICAL_UNIT_NAMES.items())
        if _qty_per_serving(unsaved, ord_) is not None
    ]


def scaled_calories(unsaved: UnsavedFoodLogEntry, canonical_servings: float) -> float | None:
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
