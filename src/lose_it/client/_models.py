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
    """Output of ``getUnsavedFoodLogEntry`` — a template before logging.

    The serving-size fields come from the food's stored ``FoodServingSize``
    record. Cross-verified against >30 distinct foods:

    - ``serving_qty`` (= ``f0``) and ``raw_qty_default`` (= ``f5``) carry
      the *default suggested log size*, not the food's per-serving
      definition.
    - ``canonical_per_serving`` (= ``f3``) and ``native_qty_per_serving``
      (= ``f4``) describe how the food is shaped: e.g. for the Trader
      Joe's "8 fl oz" tomato soup, ``f4 = 8.0 fl_oz``; for a Built Bar
      puff, ``f4 = 40.0 g``; for Core Power milkshake, ``f4 = 414 mL``.
      The ratio ``f4/f3`` is the food's per-serving quantity in its
      native unit, which is what unit-conversion math needs.
    """

    name: str
    brand: str
    category: str
    food_pk_bytes: list[int] | None
    day_key: str
    nutrients: dict[int, float] = field(default_factory=dict)
    serving_qty: float | None = None
    food_measure_ordinal: int | None = None
    food_measure_unit: str | None = None
    canonical_per_serving: float | None = None
    native_qty_per_serving: float | None = None


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
