"""Diary CRUD: ``updateFoodLogEntry`` (log) and ``deleteFoodLogEntry`` (delete).

Both methods serialize a full ``FoodLogEntry`` over the wire (the Java
object's complete state). LoseIt's API does not accept short-form "just the
PK" deletes — the server validates the body against its stored entry.
"""

from __future__ import annotations

import uuid

from .._logging import logger
from ._config import (
    DEFAULT_SERVING_SIZE_GRAMS,
    GRAMS_MEASURE_ORDINAL,
    Config,
)
from ._gwt import build_envelope, fmt_num
from ._http import HttpClient
from ._models import FoodLogEntry, UnsavedFoodLogEntry

# Of the FoodMeasurement enum, only these 9 ordinals are accepted by the
# server inside the FoodNutrients HashMap when logging an entry.
_CORE_NUTRIENT_ORDINALS = {0, 2, 3, 8, 9, 10, 11, 12, 13}


def _uuid_signed_bytes(u: uuid.UUID) -> list[int]:
    """Convert a UUID's 16 bytes to signed ints in [-128, 127]."""
    return [x - 256 if x >= 128 else x for x in u.bytes]


# ── updateFoodLogEntry (log/create) ──────────────────────────────────────────


def _build_log_payload(
    config: Config,
    unsaved: UnsavedFoodLogEntry,
    meal_ordinal: int,
    day_key: str,
    day_num: int,
    servings: float,
) -> str:
    if not unsaved.food_pk_bytes or len(unsaved.food_pk_bytes) != 16:
        raise ValueError("unsaved entry missing food primary key")

    # Send PER-SERVING nutrient values, not pre-scaled by ``servings``.
    #
    # The server applies the scaling itself by multiplying each nutrient by
    # ``FoodServing.quantity`` (the same value we already send as ``servings``).
    # Pre-scaling them here causes a double-multiplication:
    # ``displayed = (per_serving × servings) × servings = per_serving × servings²``.
    # For a 2.07-cup soup at 100 cal/serving, this turns 207 cal into 428.
    #
    # Confirmed by inspecting the official UI's outbound
    # ``updateFoodLogEntry`` payload (per-serving HashMap, e.g. cal=100)
    # against our wire dump (pre-scaled, e.g. cal=207). The UI does not
    # pre-scale.
    nutrients = {k: v for k, v in (unsaved.nutrients or {}).items() if k in _CORE_NUTRIENT_ORDINALS}
    entry_pk = _uuid_signed_bytes(uuid.uuid4())

    strings = [
        config.base_url,  # 1
        config.policy_hash,  # 2
        "com.loseit.core.client.service.LoseItRemoteService",  # 3
        "updateFoodLogEntry",  # 4
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",  # 5
        "com.loseit.core.client.model.FoodLogEntry/264522954",  # 6
        "com.loseit.core.client.model.UserId/4281239478",  # 7
        config.user_name,  # 8
        "com.loseit.core.client.model.FoodIdentifier/2763145970",  # 9
        unsaved.category or "Food",  # 10
        "en-US",  # 11
        unsaved.name,  # 12
        unsaved.brand,  # 13
        "com.loseit.core.client.model.interfaces.FoodProductType/2860616120",  # 14
        "com.loseit.healthdata.model.shared.Verification/3485154600",  # 15
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",  # 16
        "[B/3308590456",  # 17
        "com.loseit.core.client.model.FoodLogEntryContext/4082213671",  # 18
        "com.loseit.core.shared.model.DayDate/1611136587",  # 19
        "java.util.Date/3385151746",  # 20
        "com.loseit.core.client.model.interfaces.FoodLogEntryType/1152459170",  # 21
        "com.loseit.core.client.model.FoodServing/1858865662",  # 22
        "com.loseit.core.client.model.FoodNutrients/1097231324",  # 23
        "java.util.HashMap/1797211028",  # 24
        "com.loseit.healthdata.model.shared.food.FoodMeasurement/2371921172",  # 25
        "java.lang.Double/858496421",  # 26
        "com.loseit.core.client.model.FoodServingSize/63998910",  # 27
        "com.loseit.core.client.model.FoodMeasure/1457474932",  # 28
    ]
    # Default measure ordinal: 45 is a generic container-ish fallback observed
    # in the original captured replay (used when unsaved didn't carry one).
    measure_ord = unsaved.food_measure_ordinal if unsaved.food_measure_ordinal is not None else 45

    # FoodServingSize.quantity is the literal portion size the official Lose
    # It! UI renders next to the measure-unit name. Convention:
    #   • For grams (ord=8) the food's "default serving" is 100 g, so
    #     ``servings=1.2`` (1.2 servings) serializes as quantity=120 grams.
    #   • For every other measure unit the default serving is 1 unit, so
    #     quantity = servings (1.0 each, 1.5 servings, …).
    # We previously sent ``servings`` directly into the FoodServingSize slot,
    # which made gram-measured entries show up as e.g. "1.2 grams" in the
    # official mobile + web apps instead of "120 grams".
    portion_size = (
        servings * DEFAULT_SERVING_SIZE_GRAMS if measure_ord == GRAMS_MEASURE_ORDINAL else servings
    )
    servings_str = fmt_num(servings)
    portion_size_str = fmt_num(portion_size)

    parts: list[str] = ["1", "2", "3", "4", "2", "5", "6"]
    parts += ["5", "0", "7", config.user_id, "8", str(config.hours_from_gmt)]
    # FoodLogEntry header + FoodIdentifier section (category/name/brand/ProductType
    # refs, locale, then the food's own SimplePrimaryKey marker + FOOD PK).
    parts += [
        "6",
        "9",
        "-1",
        "10",
        "11",
        "12",
        "13",
        "14",
        "0",
        "-1",
        "15",
        "0",
        unsaved.day_key or day_key or "",
        "16",
        "17",
        "16",
    ]
    parts += [str(int(b)) for b in reversed(unsaved.food_pk_bytes)]
    # FoodLogEntryContext + DayDate + padding + meal type + FoodServing + nutrients
    parts += [
        "18",
        "0",
        "19",
        "20",
        day_key,
        str(day_num),
        str(config.hours_from_gmt),
        "0",
        "-1",
        "-1",
        "0",
        "0",
        "0",
        "21",
        str(meal_ordinal),
        "0",
        "22",
        "23",
        "1",
        servings_str,
        "24",
        str(len(nutrients)),
    ]
    for ord_, val in sorted(nutrients.items()):
        parts += ["25", str(ord_), "26", fmt_num(val)]
    # FoodServingSize + FoodMeasure section, then the entry's own
    # SimplePrimaryKey marker + a generated ENTRY PK. Both the FoodServingSize
    # quantity slot and the trailing FoodMeasure quantity slot are the actual
    # portion size in the food's measure unit (grams for ord=8, etc.) — not
    # the FoodServing.quantity above (which is # of servings consumed).
    parts += [
        "27",
        portion_size_str,
        "1",
        "28",
        str(int(measure_ord)),
        "1",
        "1",
        portion_size_str,
        "0",
        "P__________",
        unsaved.day_key or day_key or "",
        "16",
        "17",
        "16",
    ]
    parts += [str(int(b)) for b in reversed(entry_pk)]
    return build_envelope(strings, parts)


def log_food(
    http: HttpClient,
    unsaved: UnsavedFoodLogEntry,
    meal_ordinal: int,
    day_key: str,
    day_num: int,
    servings: float = 1.0,
) -> None:
    """Log ``unsaved`` to the given meal/day with ``servings`` portions."""
    logger.info(
        "entries.log_food: name={n!r} meal_ord={m} day_num={d} day_key={k!r} servings={s}",
        n=unsaved.name,
        m=meal_ordinal,
        d=day_num,
        k=day_key,
        s=servings,
    )
    logger.debug(
        "entries.log_food: brand={b!r} category={c!r} measure_ord={mo} "
        "unsaved.day_key={udk!r} nutrients_in={ni}",
        b=unsaved.brand,
        c=unsaved.category,
        mo=unsaved.food_measure_ordinal,
        udk=unsaved.day_key,
        ni=len(unsaved.nutrients or {}),
    )
    http.post_rpc(
        _build_log_payload(
            http.config,
            unsaved,
            meal_ordinal,
            day_key,
            day_num,
            servings,
        )
    )


# ── deleteFoodLogEntry ──────────────────────────────────────────────────────


def _build_delete_payload(config: Config, entry: FoodLogEntry) -> str:
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "deleteFoodLogEntry",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.FoodLogEntry/264522954",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        "com.loseit.core.client.model.FoodIdentifier/2763145970",
        entry.food_category or "Food",
        entry.food_name or "",
        entry.food_brand or "",
        "com.loseit.core.client.model.interfaces.FoodProductType/2860616120",
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",
        "[B/3308590456",
        "com.loseit.core.client.model.FoodLogEntryContext/4082213671",
        "com.loseit.core.shared.model.DayDate/1611136587",
        "java.util.Date/3385151746",
        "com.loseit.core.client.model.interfaces.FoodLogEntryType/1152459170",
        "com.loseit.core.client.model.interfaces.FoodLogEntryTypeExtra/4048538730",
        "com.loseit.core.client.model.FoodServing/1858865662",
        "com.loseit.core.client.model.FoodNutrients/1097231324",
        "java.util.HashMap/1797211028",
        "com.loseit.healthdata.model.shared.food.FoodMeasurement/2371921172",
        "java.lang.Double/858496421",
        "com.loseit.core.client.model.FoodServingSize/63998910",
        "com.loseit.core.client.model.FoodMeasure/1457474932",
    ]
    servings_str = fmt_num(entry.servings)
    parts: list[str] = ["1", "2", "3", "4", "2", "5", "6"]
    parts += ["5", "0", "7", config.user_id, "8", str(config.hours_from_gmt)]
    parts += [
        "6",
        "9",
        "-1",
        "10",
        "0",
        "11",
        "12",
        "13",
        "0",
        "-1",
        "0",
        entry.entry_day_key,
        "14",
        "15",
        "16",
    ]
    # FOOD PK first — this is the food's stable identifier inside the
    # FoodIdentifier object, written before the entry's context.
    parts += [str(int(b)) for b in reversed(entry.food_pk_response)]
    parts += [
        "16",
        "0",
        "17",
        "18",
        entry.context_day_key,
        str(entry.day_num),
        str(entry.hours_from_gmt),
        "0",
        "-1",
        "1",
        "0",
        "0",
        "0",
        "19",
        str(entry.meal_ordinal),
        "20",
        str(entry.extra_ordinal),
        "21",
        "22",
        servings_str,
        servings_str,
        "23",
        str(len(entry.nutrients_ordered)),
    ]
    for ord_, val in entry.nutrients_ordered:
        parts += ["24", str(int(ord_)), "25", fmt_num(val)]
    parts += [
        "26",
        servings_str,
        "0",
        "27",
        str(entry.food_measure_ordinal),
        servings_str,
        servings_str,
        servings_str,
        "0",
        entry.food_identifier_code,
        entry.entry_day_key,
        "14",
        "15",
        "16",
    ]
    # ENTRY PK last — this is the FoodLogEntry's own SimplePrimaryKey (a UUID),
    # serialized at the end of the object.
    parts += [str(int(b)) for b in reversed(entry.entry_pk_response)]
    return build_envelope(strings, parts)


def delete(http: HttpClient, entry: FoodLogEntry) -> None:
    """Delete a diary entry. The whole entry payload is required by the server."""
    logger.info(
        "entries.delete: name={n!r} meal_ord={m} day_num={d} servings={s}",
        n=entry.food_name,
        m=entry.meal_ordinal,
        d=entry.day_num,
        s=entry.servings,
    )
    logger.debug(
        "entries.delete: brand={b!r} category={c!r} measure_ord={mo} "
        "food_id_code={fic!r} entry_day_key={edk!r} context_day_key={cdk!r}",
        b=entry.food_brand,
        c=entry.food_category,
        mo=entry.food_measure_ordinal,
        fic=entry.food_identifier_code,
        edk=entry.entry_day_key,
        cdk=entry.context_day_key,
    )
    http.post_rpc(_build_delete_payload(http.config, entry))
