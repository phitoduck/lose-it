"""Dataclass models for SDK return values.

These mirror the relevant LoseIt domain objects but keep only the fields
needed for the round-trip (search → unsaved → log; list → delete).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FoodSearchResult:
    """One row from ``searchFoods``."""

    name: str
    brand: str
    category: str
    pk_bytes: list[int]  # in response form (already reversed from wire)


@dataclass
class UnsavedFoodLogEntry:
    """Output of ``getUnsavedFoodLogEntry`` — a template before logging."""

    name: str
    brand: str
    category: str
    food_pk_bytes: list[int] | None
    day_key: str
    nutrients: dict[int, float] = field(default_factory=dict)
    serving_qty: float | None = None
    food_measure_ordinal: int | None = None


@dataclass
class FoodLogEntry:
    """A logged diary entry as returned from ``getDailyDetailsIncludingPendingForDate``.

    Holds everything needed to construct a ``deleteFoodLogEntry`` payload.
    Both PK byte arrays are stored in **response form**; the SDK reverses
    them when building outbound requests.
    """

    food_category: str
    food_name: str
    food_brand: str
    food_pk_response: list[int]
    entry_pk_response: list[int]
    entry_day_key: str
    context_day_key: str
    day_num: int
    hours_from_gmt: int
    meal_ordinal: int
    extra_ordinal: int
    food_measure_ordinal: int
    servings: float
    food_identifier_code: str
    # The order is significant — Java's HashMap iteration order, preserved from server.
    nutrients_ordered: list[tuple[int, float]] = field(default_factory=list)

    @property
    def calories(self) -> float | None:
        for ord_, val in self.nutrients_ordered:
            if ord_ == 0:
                return val
        return None
