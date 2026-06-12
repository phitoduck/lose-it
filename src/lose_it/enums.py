"""Public enums for SDK + CLI use.

Lose It! has two domain enumerations the CLI and SDK both lean on:

- :class:`MealType` — which slot of the day an entry was logged to.
  Values mirror the wire protocol's ``meal_ordinal`` (0..3), so a
  :class:`MealType` can be passed directly anywhere an ordinal is
  expected.
- :class:`ServingUnit` — the canonical lowercase names for the
  ``FoodMeasurement`` enum exposed at the user-facing surface (``cup``,
  ``mL``, ``g``, ``serving``, …).

Both are re-exported from :mod:`lose_it` so SDK callers can write::

    from lose_it import LoseIt, MealType, ServingUnit

    with LoseIt.from_env() as li:
        li.log_food(food, meal=MealType.lunch, serving_unit=ServingUnit.cup,
                    serving_amount=1.0)

The CLI's ``--meal`` / ``--serving-unit`` flags also accept the enum
member name (case-insensitive). Strings remain accepted at the API
boundary; the SDK normalizes them internally.

``Browser`` (chrome/brave) is *not* here — it's purely a CLI affordance
for ``--browser`` choice display. The SDK takes a plain string
(``LoseIt.login_from_browser(browser="chrome")``) because at the SDK
layer there's nothing browser-shaped about it; it's just the cookie
store path.
"""

from __future__ import annotations

import enum

__all__ = ["MealType", "ServingUnit"]


class MealType(enum.IntEnum):
    """Which meal slot a diary entry belongs to.

    Values are the wire-protocol ``meal_ordinal`` integers, so a
    :class:`MealType` member can be passed wherever an ordinal is
    expected without ``.value`` dereferencing::

        meal_ord: int = MealType.lunch  # 1

    Use :meth:`parse` to accept user input — handles the singular
    ``"snack"`` alias and case-insensitive matching.
    """

    breakfast = 0
    lunch = 1
    dinner = 2
    snacks = 3

    @classmethod
    def parse(cls, value: MealType | str | int) -> MealType:
        """Coerce a flexible meal identifier to a :class:`MealType`.

        Accepts:

        - A :class:`MealType` (returned unchanged).
        - The enum member name, case-insensitive: ``"Lunch"``, ``"LUNCH"``.
        - The singular alias ``"snack"`` → :attr:`snacks`.
        - The raw ordinal as ``int`` (0..3).

        Raises:
            ValueError: ``value`` does not match any known meal.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            key = value.strip().lower()
            if key == "snack":
                return cls.snacks
            for member in cls:
                if member.name == key:
                    return member
        valid = ", ".join(m.name for m in cls)
        raise ValueError(f"Unknown meal {value!r}; expected one of {{{valid}}} or 'snack'.")


class ServingUnit(enum.StrEnum):
    """Canonical lowercase unit names accepted by ``--serving-unit``.

    Members are the canonical names from
    :data:`lose_it.core._units.CANONICAL_UNIT_NAMES`. Common aliases
    (``cups``, ``tablespoons``, ``milliliter``, …) are still handled by
    :func:`lose_it.core._units.resolve_unit` for callers that pass raw
    strings; the typed enum here pins the *display* surface.

    Backed by ``str`` so a :class:`ServingUnit` is interchangeable with
    its canonical string (e.g. ``resolve_unit(ServingUnit.cup)`` works).
    """

    tsp = "tsp"
    tbsp = "tbsp"
    cup = "cup"
    piece = "piece"
    each = "each"
    g = "g"
    fl_oz = "fl_oz"
    mL = "mL"
    bottle = "bottle"
    can = "can"
    slice = "slice"
    serving = "serving"
    scoop = "scoop"
    container = "container"
    pie = "pie"
