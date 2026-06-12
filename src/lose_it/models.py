"""Dataclass models for SDK return values.

These mirror the relevant LoseIt domain objects but keep only the fields
needed for the round-trip (search → unsaved → log; list → delete).

Two flavors live here:

- *Wire-shape* models (``FoodSearchResult``, ``UnsavedFoodLogEntry``,
  ``FoodLogEntry``) — direct projections of LoseIt's GWT-RPC payloads;
  consumed by the low-level ``foods``/``entries``/``daily`` modules.
- *High-level result* models (``FoodDescription``, ``LoggedFood``,
  ``LoginResult``) — synthetic dataclasses returned by the
  :class:`~lose_it.LoseIt` client's convenience methods; composed of
  wire-shape values plus derived/formatted fields so callers don't
  have to reach into the lower-level types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
    # Labeled nutrients — same data as ``nutrients`` but with the
    # FoodNutrient enum applied: ``{"calories": 100, "sodium_mg": 140,
    # "unknown_nutrient_18": 30, ...}``. Surfaced in ``lose-it -o json``
    # output and in ``describe-food``.
    nutrients_by_label: dict[str, float] = field(default_factory=dict)
    # Cross-class unit conversion. Extracted from the food's nutrient
    # HashMap when present:
    #   - ``per_serving_ml`` = value at FoodNutrient.SERVING_VOLUME_ML (ord=1)
    #     — present only for volumetric foods (cup/fl_oz/mL native)
    #   - ``per_serving_g``  = value at FoodNutrient.SERVING_WEIGHT_G (ord=2)
    #     — present only for mass foods (gram-stored)
    # These let the log path convert e.g. ``--serving-amount 152 --serving-unit g``
    # against an ord=27 (serving)-stored chicken-strips food without a generic
    # serving→grams entry in CONVERSIONS.
    per_serving_ml: float | None = None
    per_serving_g: float | None = None


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


# ── High-level result models (LoseIt client method returns) ─────────────────


@dataclass(frozen=True)
class PrimaryServing:
    """The food's stored "1 serving" definition.

    ``native_qty_per_serving`` is the raw quantity in ``unit`` (e.g. 40.0
    when ``unit='g'`` for a Built Bar, or 8.0 when ``unit='fl_oz'`` for a
    cup of soup). ``canonical_per_serving`` is the FoodServingSize
    denominator — almost always 1.0; needed only for foods that ship
    fractional serving definitions.
    """

    ordinal: int | None
    unit: str | None
    canonical_per_serving: float | None
    native_qty_per_serving: float | None


@dataclass(frozen=True)
class CrossClassConversion:
    """Per-food gram / mL totals exposed in the FoodNutrients HashMap.

    These let the portion resolver convert e.g. ``152 g`` against a
    serving-stored food (chicken strips) without a generic
    serving→g entry in the unit table. ``None`` when the food doesn't
    carry the conversion (most volume-only or count-only foods).
    """

    per_serving_g: float | None
    per_serving_ml: float | None


@dataclass(frozen=True)
class FoodDescription:
    """Output of ``LoseIt.describe_food`` — full nutrient/serving profile.

    Same data the ``loseit describe-food`` command renders. Fold into
    JSON with :func:`lose_it.core._formatters.food_description_to_dict`,
    or pretty-print with :func:`render_food_description`.
    """

    food_id: str
    name: str
    brand: str
    category: str
    primary_serving: PrimaryServing
    cross_class_conversion: CrossClassConversion
    nutrients_per_serving: dict[str, float]
    raw_nutrients_by_ord: dict[int, float]


@dataclass(frozen=True)
class LoggedFood:
    """Result of ``LoseIt.log_food`` (or its dry-run equivalent).

    Bundles the food that was chosen, the portion-size shape that hit the
    wire, and the scaled calorie total so callers don't have to multiply
    per-serving cals × servings themselves. ``dry_run=True`` means no
    ``updateFoodLogEntry`` call was made; everything else is identical.
    """

    food: FoodSearchResult
    meal_ordinal: int
    meal_name: str
    when: str  # ISO ``YYYY-MM-DD``
    canonical_servings: float
    portion_amount: float
    portion_unit: str
    calories: float | None
    dry_run: bool


@dataclass(frozen=True)
class LoginResult:
    """Result of ``LoseIt.login_from_browser``.

    ``status`` is ``"ok"`` when a fresh token was imported, or one of
    ``"missing"``/``"expired"`` when the browser cookie couldn't supply a
    usable JWT. ``config_values`` is populated only when
    ``write_config=True`` AND user_id/user_name/hours_from_gmt all
    resolved (else ``None`` — the caller decides whether to prompt or
    surface the partial result).
    """

    status: str  # "ok" | "missing" | "expired"
    browser: str
    token_file: Path
    exp: int | None
    exp_iso: str | None
    config_file: Path | None
    config_values: dict[str, object] | None
    signin_url: str | None = None  # set on status != "ok" so the CLI can offer it
    message: str | None = None
