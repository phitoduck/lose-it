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

Every model exposes ``.to_dict()`` — the JSON-safe projection used by
``loseit --output json``/``toon`` and by any caller that wants the
flattened shape without writing the field walk themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .core._enums import label_for_nutrient, label_for_ordinal
from .core._ids import pk_to_hex
from .enums import MealType


@dataclass
class FoodSearchResult:
    """One row from ``searchFoods``."""

    name: str
    brand: str
    category: str
    pk_bytes: list[int]  # in response form (already reversed from wire)

    @property
    def food_id(self) -> str:
        """Lowercase-hex form of :attr:`pk_bytes` (32 chars), or ``""``.

        The 32-char form is the only food identifier the CLI accepts —
        :class:`~lose_it.LoseIt`'s ``log_food`` / ``get_food`` /
        ``describe_food`` all take it. Returns an empty string when
        ``pk_bytes`` isn't a 16-byte key (e.g. partial fixtures).
        """
        return pk_to_hex(self.pk_bytes) if len(self.pk_bytes) == 16 else ""

    def to_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        """Project to a JSON-safe dict.

        Shape: ``{"name", "brand", "category", "food_id", ?"pk_bytes"}``.
        ``verbose=True`` adds the raw 16-int ``pk_bytes`` list — useful
        when round-tripping the result back to a low-level RPC, noisy
        in CLI output.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "brand": self.brand,
            "category": self.category,
            "food_id": self.food_id,
        }
        if verbose:
            out["pk_bytes"] = list(self.pk_bytes)
        return out


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
    # Server-side audit timestamps. ``created_at`` is the FoodLogEntry's
    # original log time (FLE.f4 epoch-ms long, stored UTC server-side);
    # ``modified_at`` is the last edit time (FLE.f5). They are the
    # join key for the backup upsert restore mode (spec §4.4) — stable
    # across food-metadata edits, drift-tolerant via a ±10 minute
    # window. Default ``None`` keeps the dataclass instantiable from
    # fixtures captured before this surface landed.
    created_at: datetime | None = None
    modified_at: datetime | None = None

    @property
    def calories(self) -> float | None:
        for ord_, val in self.nutrients_ordered:
            if ord_ == 0:
                return val
        return None

    @property
    def food_id(self) -> str:
        """Lowercase-hex form of :attr:`food_pk_response` (32 chars), or ``""``.

        Same encoding as :attr:`FoodSearchResult.food_id`, so the value
        round-trips back through ``hex_to_pk`` / ``LoseIt.get_food`` /
        ``log_food(food_id=...)``. The entry PK has no equivalent surface —
        no LoseIt RPC accepts it as input on its own — so it is intentionally
        kept internal to the SDK rather than exposed alongside ``food_id``.
        """
        return pk_to_hex(self.food_pk_response) if len(self.food_pk_response) == 16 else ""

    @property
    def meal_name(self) -> str:
        """Human-readable meal name (``"lunch"``, etc.) for :attr:`meal_ordinal`.

        Falls back to ``"meal<N>"`` for ordinals outside the documented
        0..3 range so the projection stays informative even if Lose It!
        ever ships a new slot.
        """
        try:
            return MealType(self.meal_ordinal).name
        except ValueError:
            return f"meal{self.meal_ordinal}"

    @property
    def food_measure_unit(self) -> str:
        """Label for :attr:`food_measure_ordinal` (``"grams"``, ``"each"``, …).

        Uses the same FoodMeasurement-ordinal → label mapping the
        decoder uses for typed enum values.
        """
        return label_for_ordinal(self.food_measure_ordinal)

    @property
    def nutrients_by_label(self) -> dict[str, float]:
        """Labeled view of :attr:`nutrients_ordered`.

        Each ordinal gets mapped through :func:`label_for_nutrient`
        (``0 → "calories"``, ``9 → "sodium_mg"``, …), so downstream
        callers don't have to remember the ordinal table.
        """
        return {label_for_nutrient(int(ord_)): float(val) for ord_, val in self.nutrients_ordered}

    def to_dict(self) -> dict[str, Any]:
        """Project to a JSON-safe dict.

        Carries both the raw-ordinal nutrient map and the labeled
        nutrient map so the document is both human-readable and
        machine-parseable. Used by ``loseit diary --output json``.
        """
        raw_nutrients = {int(ord_): float(val) for ord_, val in self.nutrients_ordered}
        return {
            "meal": self.meal_name,
            "meal_ordinal": self.meal_ordinal,
            "food_name": self.food_name,
            "food_brand": self.food_brand,
            "food_category": self.food_category,
            "food_identifier_code": self.food_identifier_code,
            "servings": self.servings,
            "calories": self.calories,
            "nutrients": raw_nutrients,
            "nutrients_by_label": self.nutrients_by_label,
            "food_id": self.food_id,
            "entry_day_key": self.entry_day_key,
            "context_day_key": self.context_day_key,
            "day_num": self.day_num,
            "food_measure_ordinal": self.food_measure_ordinal,
            "food_measure_unit": self.food_measure_unit,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
        }


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

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (same field names as the dataclass)."""
        return {
            "ordinal": self.ordinal,
            "unit": self.unit,
            "canonical_per_serving": self.canonical_per_serving,
            "native_qty_per_serving": self.native_qty_per_serving,
        }


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

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (same field names as the dataclass)."""
        return {
            "per_serving_g": self.per_serving_g,
            "per_serving_ml": self.per_serving_ml,
        }


@dataclass(frozen=True)
class FoodDescription:
    """Output of ``LoseIt.describe_food`` — full nutrient/serving profile.

    Same data the ``loseit describe-food`` command renders. Call
    :meth:`to_dict` for the JSON shape used by ``--output json``/``toon``.
    """

    food_id: str
    name: str
    brand: str
    category: str
    primary_serving: PrimaryServing
    cross_class_conversion: CrossClassConversion
    nutrients_per_serving: dict[str, float]
    raw_nutrients_by_ord: dict[int, float]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection — nested under ``primary_serving`` /
        ``cross_class_conversion`` for symmetry with the dataclass."""
        return {
            "food_id": self.food_id,
            "name": self.name,
            "brand": self.brand,
            "category": self.category,
            "primary_serving": self.primary_serving.to_dict(),
            "cross_class_conversion": self.cross_class_conversion.to_dict(),
            "nutrients_per_serving": dict(self.nutrients_per_serving),
            "raw_nutrients_by_ord": dict(self.raw_nutrients_by_ord),
        }


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

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection. Mirrors the ``loseit log --output json``
        envelope: ``action="log"`` at the top so consumers can pattern-match
        the kind of event regardless of which command produced it."""
        return {
            "action": "log",
            "dry_run": self.dry_run,
            "date": self.when,
            "meal": self.meal_name,
            "meal_ordinal": self.meal_ordinal,
            "servings": self.canonical_servings,
            "portion_size": self.portion_amount,
            "measure_unit": self.portion_unit,
            "food": self.food.to_dict(),
            "calories": self.calories,
        }


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

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection — mirrors the ``loseit login --output json``
        envelope. Optional fields (``signin_url``, ``message``) only appear
        when set."""
        out: dict[str, Any] = {
            "action": "login",
            "status": self.status,
            "browser": self.browser,
            "token_file": str(self.token_file),
            "exp": self.exp,
            "exp_iso": self.exp_iso,
            "config_file": str(self.config_file) if self.config_file is not None else None,
            "config_values": self.config_values,
        }
        if self.signin_url is not None:
            out["signin_url"] = self.signin_url
        if self.message is not None:
            out["message"] = self.message
        return out
