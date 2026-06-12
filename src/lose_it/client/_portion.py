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

from ._models import UnsavedFoodLogEntry

__all__ = ["PortionError", "ResolvedPortion", "resolve_portion", "scaled_calories"]


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


def resolve_portion(
    unsaved: UnsavedFoodLogEntry,
    servings: float = 1.0,
    serving_amount: float | None = None,
    serving_unit: str | None = None,
) -> ResolvedPortion:
    """Validate the portion knobs and resolve them to wire + display values.

    Decision tree:

    1. If both ``serving_amount`` and ``serving_unit`` are ``None``,
       this is the legacy ``--servings N`` path; return the raw multiplier
       and render in the food's native unit.
    2. Otherwise both must be set and ``servings`` must be left at its
       default of ``1.0`` (passing both is treated as user confusion).
    3. Resolve ``serving_unit`` (string or alias) to its FoodMeasurement
       ordinal via :func:`lose_it.client._units.resolve_unit`.
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
    raise NotImplementedError


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
    raise NotImplementedError
