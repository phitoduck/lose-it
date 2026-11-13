"""Recipe creation: ``saveRecipe(ServiceRequestToken, Recipe)``.

The web client persists a recipe as one object graph — the recipe's own
``ActiveFood`` (name, aggregate nutrients, serving size) plus an array of
``RecipeIngredient`` rows, each carrying a full ``FoodIdentifier`` and
``FoodServing``. There is no per-ingredient RPC: adding, editing or
removing an ingredient in the UI re-sends the whole recipe. So creating a
recipe with N ingredients is a single ``saveRecipe`` call.

Wire shape, decoded from the web client's outbound payloads against the
serializers in the JS bundle (``Recipe`` = ``f6g``, ``RecipeIngredient``
= ``U5g``, ``ActiveFood`` = ``Rxg`` in permutation 201A8433…)::

    Recipe
      a  ActiveFood
           d  int            -1 for a new recipe
           e  FoodIdentifier (category "Recipe", recipe name, recipe PK)
           f  FoodServing    FoodNutrients(1, 1, <whole-recipe totals>)
                             FoodServingSize(1, true, <unit>, 1, N, N)
           g  HourDate       (today's UTC midnight + 1s, hours since 2001-01-01)
           i  bool 0 · j int 0 · k bool 1 · n bool 0
           u  long           now (ms)
           v  SimplePrimaryKey (recipe PK)
      b  String null · c Timestamp null · d bool 0
      e  double         N — the serving-size quantity ("4 Servings")
      f  String null
      g  RecipeIngredient[]
      i  bool 1 · j String null · k, n Object null
      o  FoodMeasure    serving-size unit (27 = Serving)
      p  bool 0
      u  long           now (ms)
      v  SimplePrimaryKey (recipe PK)

    RecipeIngredient
      a  bool 0
      b  FoodIdentifier — same shape ``updateFoodLogEntry`` sends
      c  FoodServing    FoodNutrients(1, servings, <PER-SERVING nutrients>)
                        FoodServingSize(servings, true, <unit>, d, e, qty)
      d  int -1 · e bool 0 · f int -1
      g  SimplePrimaryKey (the parent recipe's PK)
      u  long now
      v  SimplePrimaryKey (fresh ingredient PK)

``FoodServingSize.e / .d`` is the chosen unit's quantity per canonical
serving and ``.f`` is the quantity in that unit — e.g. 3 large eggs on a
100 g-per-serving food: ``(1.5, true, Each, 0.5, 1, 3)``.

Serving-size semantics: ``FoodServingSize(1, …, Serving, 1, N, N)`` on
the recipe's ``ActiveFood`` says "the whole recipe = N servings", so
logging *1 serving* of the recipe later yields totals / N.

Object back-references (negative tokens) are an encoder optimisation in
GWT; the server accepts fresh copies too — the web client itself repeats
the recipe PK as a new object in every ingredient — so this builder
never emits them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .._logging import logger
from ..models import UnsavedFoodLogEntry
from ._config import Config
from ._gwt import build_envelope, fmt_num
from ._http import HttpClient

# FoodMeasurement ordinal for the generic "Serving" unit.
SERVING_MEASURE_ORDINAL = 27

# Lose It!'s HourDate counts hours from this epoch (the same anchor the
# diary's day numbers use).
_HOUR_DATE_EPOCH = date(2001, 1, 1)

_LONG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$_"

_T_SERVICE_TOKEN = "com.loseit.core.client.service.ServiceRequestToken/1076571655"
_T_RECIPE = "com.loseit.core.client.model.Recipe/643213359"
_T_USER_ID = "com.loseit.core.client.model.UserId/4281239478"
_T_ACTIVE_FOOD = "com.loseit.core.client.model.ActiveFood/640079221"
_T_FOOD_IDENTIFIER = "com.loseit.core.client.model.FoodIdentifier/2763145970"
_T_PRODUCT_TYPE = "com.loseit.core.client.model.interfaces.FoodProductType/2860616120"
_T_VERIFICATION = "com.loseit.healthdata.model.shared.Verification/3485154600"
_T_PRIMARY_KEY = "com.loseit.core.client.model.SimplePrimaryKey/3621315060"
_T_BYTE_ARRAY = "[B/3308590456"
_T_FOOD_SERVING = "com.loseit.core.client.model.FoodServing/1858865662"
_T_FOOD_NUTRIENTS = "com.loseit.core.client.model.FoodNutrients/1097231324"
_T_HASH_MAP = "java.util.HashMap/1797211028"
_T_MEASUREMENT = "com.loseit.healthdata.model.shared.food.FoodMeasurement/2371921172"
_T_DOUBLE = "java.lang.Double/858496421"
_T_SERVING_SIZE = "com.loseit.core.client.model.FoodServingSize/63998910"
_T_FOOD_MEASURE = "com.loseit.core.client.model.FoodMeasure/1457474932"
_T_HOUR_DATE = "com.loseit.core.client.model.HourDate/83922329"
_T_DATE = "java.util.Date/3385151746"
_T_INGREDIENT_ARRAY = "[Lcom.loseit.core.client.model.RecipeIngredient;/2059606128"
_T_INGREDIENT = "com.loseit.core.client.model.RecipeIngredient/3403312587"


@dataclass(frozen=True)
class IngredientLine:
    """One resolved ingredient, ready to serialize.

    ``measure_ordinal`` / ``per_serving_denominator`` /
    ``unit_qty_per_serving`` / ``quantity`` map 1:1 onto the ingredient's
    ``FoodServingSize`` fields ``c`` / ``d`` / ``e`` / ``f``.
    """

    unsaved: UnsavedFoodLogEntry
    servings: float
    measure_ordinal: int
    per_serving_denominator: float
    unit_qty_per_serving: float
    quantity: float


def encode_long(value: int) -> str:
    """Encode a non-negative int as a GWT base64 long (inverse of ``_decode_long``)."""
    if value < 0:
        raise ValueError("negative longs are not supported")
    if value == 0:
        return "A"
    chars: list[str] = []
    while value:
        chars.append(_LONG_ALPHABET[value & 63])
        value >>= 6
    return "".join(reversed(chars))


def _uuid_bytes() -> list[int]:
    return [b - 256 if b >= 128 else b for b in uuid.uuid4().bytes]


def recipe_totals(lines: list[IngredientLine]) -> dict[int, float]:
    """Sum each ingredient's per-serving nutrients × servings, per ordinal."""
    totals: dict[int, float] = {}
    for line in lines:
        for ordinal, value in (line.unsaved.nutrients or {}).items():
            totals[ordinal] = totals.get(ordinal, 0.0) + value * line.servings
    return totals


class _Writer:
    """Accumulates a GWT-RPC request: string table + data tokens."""

    def __init__(self) -> None:
        self.strings: list[str] = []
        self._index: dict[str, int] = {}
        self.data: list[str] = []

    def ref(self, s: str | None) -> str:
        """String-table reference for ``s`` (``"0"`` encodes ``null``)."""
        if s is None:
            return "0"
        if s not in self._index:
            self.strings.append(s)
            self._index[s] = len(self.strings)
        return str(self._index[s])

    def put(self, *tokens: str) -> None:
        self.data.extend(tokens)

    def string(self, s: str | None) -> None:
        self.put(self.ref(s))

    def obj(self, type_sig: str) -> None:
        self.put(self.ref(type_sig))

    def primary_key(self, pk: list[int]) -> None:
        self.obj(_T_PRIMARY_KEY)
        self.obj(_T_BYTE_ARRAY)
        self.put(str(len(pk)), *(str(int(b)) for b in pk))

    def measure(self, ordinal: int) -> None:
        self.obj(_T_FOOD_MEASURE)
        self.put(str(int(ordinal)))

    def nutrients(self, base: float, servings: float, values: dict[int, float]) -> None:
        self.obj(_T_FOOD_NUTRIENTS)
        self.put(fmt_num(base), fmt_num(servings))
        self.obj(_T_HASH_MAP)
        self.put(str(len(values)))
        for ordinal, value in sorted(values.items()):
            self.obj(_T_MEASUREMENT)
            self.put(str(int(ordinal)))
            self.obj(_T_DOUBLE)
            self.put(fmt_num(value))

    def serving_size(self, a: float, measure_ordinal: int, d: float, e: float, f: float) -> None:
        self.obj(_T_SERVING_SIZE)
        self.put(fmt_num(a), "1")
        self.measure(measure_ordinal)
        self.put(fmt_num(d), fmt_num(e), fmt_num(f))


def _hour_date(today: date) -> tuple[str, int]:
    """``(Date long, hour count)`` for an ``HourDate`` pinned to ``today``.

    The web client sends today's UTC midnight + 1 s and the matching
    whole-day hour count (e.g. ``226704`` for 2026-11-12).
    """
    midnight = datetime(today.year, today.month, today.day, tzinfo=UTC)
    millis = int((midnight + timedelta(seconds=1)).timestamp() * 1000)
    hours = (today - _HOUR_DATE_EPOCH).days * 24
    return encode_long(millis), hours


def build_save_recipe_payload(
    config: Config,
    name: str,
    lines: list[IngredientLine],
    servings: float = 1.0,
    *,
    recipe_pk: list[int] | None = None,
    ingredient_pks: list[list[int]] | None = None,
    now: datetime | None = None,
) -> str:
    """Build the ``saveRecipe`` envelope for a brand-new recipe.

    ``servings`` is how many servings the whole recipe yields. The PK and
    clock arguments exist for deterministic tests; production callers
    leave them ``None`` (fresh UUIDs, current time).
    """
    if not name.strip():
        raise ValueError("recipe name must not be empty")
    if servings <= 0:
        raise ValueError(f"recipe servings must be positive (got {servings})")
    if not lines:
        raise ValueError("a recipe needs at least one ingredient")
    for line in lines:
        if not line.unsaved.food_pk_bytes or len(line.unsaved.food_pk_bytes) != 16:
            raise ValueError(f"ingredient {line.unsaved.name!r} is missing its food primary key")

    recipe_pk = recipe_pk or _uuid_bytes()
    if ingredient_pks is None:
        ingredient_pks = [_uuid_bytes() for _ in lines]
    if len(ingredient_pks) != len(lines):
        raise ValueError("ingredient_pks must have one entry per ingredient")
    now = now or datetime.now(UTC)
    now_long = encode_long(int(now.timestamp() * 1000))
    hour_date_long, hour_count = _hour_date(now.date())

    w = _Writer()
    # Method header: 1|2|3|4|<2 params>|<param type sigs>
    w.string(config.base_url)
    w.string(config.policy_hash)
    w.string("com.loseit.core.client.service.LoseItRemoteService")
    w.string("saveRecipe")
    w.put("2")
    w.string(_T_SERVICE_TOKEN)
    w.string(_T_RECIPE)

    # ServiceRequestToken(a=null, b=UserId(id, name, hoursFromGmt))
    w.obj(_T_SERVICE_TOKEN)
    w.put("0")
    w.obj(_T_USER_ID)
    w.put(str(config.user_id))
    w.string(config.user_name)
    w.put(str(config.hours_from_gmt))

    # ── Recipe ───────────────────────────────────────────────────────────
    w.obj(_T_RECIPE)
    # a: ActiveFood
    w.obj(_T_ACTIVE_FOOD)
    w.put("-1")
    w.obj(_T_FOOD_IDENTIFIER)
    w.put("-1")
    w.string("Recipe")  # category
    w.put("0")  # locale: null
    w.string(name)
    w.string("")  # brand
    w.obj(_T_PRODUCT_TYPE)
    w.put("0", "-1", "0", "A")  # product type ord, g, verification=null, u
    w.primary_key(recipe_pk)
    w.obj(_T_FOOD_SERVING)
    w.nutrients(1, 1, recipe_totals(lines))
    w.serving_size(1, SERVING_MEASURE_ORDINAL, 1, servings, servings)
    w.obj(_T_HOUR_DATE)
    w.obj(_T_DATE)
    w.put(hour_date_long, str(hour_count))
    w.put("0", "0", "1", "0", now_long)  # i, j, k, n, u
    w.primary_key(recipe_pk)
    # b..f
    w.put("0", "0", "0", fmt_num(servings), "0")
    # g: RecipeIngredient[]
    w.obj(_T_INGREDIENT_ARRAY)
    w.put(str(len(lines)))
    for line, ingredient_pk in zip(lines, ingredient_pks, strict=True):
        u = line.unsaved
        w.obj(_T_INGREDIENT)
        w.put("0")
        w.obj(_T_FOOD_IDENTIFIER)
        w.put("-1")
        w.string(u.category or "Food")
        w.string("en-US")
        w.string(u.name)
        w.string(u.brand or None)
        w.obj(_T_PRODUCT_TYPE)
        w.put("0", "-1")
        w.obj(_T_VERIFICATION)
        w.put("0", u.day_key or "A")
        # Food PK goes out reversed, same as updateFoodLogEntry.
        w.primary_key(list(reversed(u.food_pk_bytes or [])))
        w.obj(_T_FOOD_SERVING)
        w.nutrients(1, line.servings, dict(u.nutrients or {}))
        w.serving_size(
            line.servings,
            line.measure_ordinal,
            line.per_serving_denominator,
            line.unit_qty_per_serving,
            line.quantity,
        )
        w.put("-1", "0", "-1")
        w.primary_key(recipe_pk)
        w.put(now_long)
        w.primary_key(ingredient_pk)
    # i, j, k, n
    w.put("1", "0", "0", "0")
    # o: serving-size unit
    w.measure(SERVING_MEASURE_ORDINAL)
    # p, u, v
    w.put("0", now_long)
    w.primary_key(recipe_pk)

    return build_envelope(w.strings, w.data)


def save_recipe(
    http: HttpClient,
    name: str,
    lines: list[IngredientLine],
    servings: float = 1.0,
) -> str:
    """Create a recipe with ``lines`` as its ingredients. Returns the raw response."""
    logger.info(
        "recipes.save_recipe: name={n!r} ingredients={k} servings={s}",
        n=name,
        k=len(lines),
        s=servings,
    )
    return http.post_rpc(build_save_recipe_payload(http.config, name, lines, servings))
