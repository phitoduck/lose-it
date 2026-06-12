"""Food lookup RPCs: ``searchFoods`` and ``getUnsavedFoodLogEntry``.

``searchFoods`` is the autocomplete-style endpoint: send a query, get back
candidate foods with their primary keys.

``getUnsavedFoodLogEntry`` is the second step before logging — it returns
the food's serving size + nutrient template, which the client then scales
by the desired number of servings when posting to ``updateFoodLogEntry``.

Response parsing is delegated to :mod:`lose_it.client._decoder`,
which walks the GWT TypeSerializer schemas extracted from Lose It!'s
compiled JS bundle. The schemas encode field order + type for every
class on the wire, so positional access (``f0``, ``f1``, …) corresponds
to declaration order in the Java source — no string-length heuristics
required.
"""

from __future__ import annotations

from typing import Any

from .._logging import logger
from ._config import Config
from ._decoder import decode_response
from ._gwt import build_envelope
from ._http import HttpClient, LoseItError
from ._ids import pk_to_hex
from ._models import FoodSearchResult, UnsavedFoodLogEntry

# ── Schema field positions ───────────────────────────────────────────────────
#
# The schemas in ``_schemas.json`` give us POSITIONAL field types but not
# semantic names. The mappings below were derived empirically by running
# real captured responses through ``decode_response`` and pattern-matching
# the values against what the live Lose It! UI shows. Each mapping is
# pinned to the FQCN+hash, so a schema change (Lose It! redeploy that
# alters a class) would invalidate them — but the hash in the FQCN would
# change too, surfacing the issue at the schema-extraction stage.

_SEARCH_RESULT_FOOD = "com.loseit.core.client.model.search.SearchResultFood/3343986608"
_SEARCH_RESULTS = "com.loseit.core.client.model.search.SearchResults/1258509077"
_SIMPLE_PK = "com.loseit.core.client.model.SimplePrimaryKey/3621315060"
_FOOD_IDENTIFIER = "com.loseit.core.client.model.FoodIdentifier/2763145970"
_UNSAVED_FOOD_LOG_ENTRY = "com.loseit.core.client.model.FoodLogEntry/264522954"


# ── searchFoods ─────────────────────────────────────────────────────────────


def _build_search_payload(config: Config, query: str) -> str:
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "searchFoods",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "java.lang.String/2004016611",
        "I",
        "Z",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        query,
        "en-US",
    ]
    data = (
        f"1|2|3|4|6|5|6|6|7|8|8|5|0|9|{config.user_id}|10|{config.hours_from_gmt}|11|12|15|1|1"
    ).split("|")
    return build_envelope(strings, data)


def _walk(root: Any, fqcn_prefix: str | None = None, fqcn: str | None = None):
    """Yield every dict node in ``root`` whose ``__type__`` matches.

    Either pass an exact ``fqcn`` (full FQCN+hash) or a ``fqcn_prefix``
    (everything before the ``/<hash>``). Walks lists, dicts, and the
    ``items`` / ``f*`` slots emitted by the decoder.
    """

    def match(t: str) -> bool:
        if fqcn is not None:
            return t == fqcn
        if fqcn_prefix is not None:
            return t.startswith(fqcn_prefix)
        return False

    if isinstance(root, dict):
        t = root.get("__type__")
        if isinstance(t, str) and match(t):
            yield root
        for v in root.values():
            yield from _walk(v, fqcn_prefix, fqcn)
    elif isinstance(root, list):
        for v in root:
            yield from _walk(v, fqcn_prefix, fqcn)


def _extract_pk_bytes(pk_field: Any) -> list[int]:
    """SimplePrimaryKey wraps a raw byte[] in its sole field.

    Returns the bytes in **response form** — i.e. the order the OLD
    heuristic parser produced, which is the order the SDK's outbound
    payload builders (``_build_unsaved_payload`` etc.) expect before
    they apply their own ``reversed(...)`` for the wire.

    The schema-driven decoder pops bytes LIFO from the token stack, so
    its internal byte order is the *opposite* of the on-stream slice the
    old parser captured. We flip it once here to match the existing
    pk_bytes contract — without this flip, every request that references
    the food's PK (``getUnsavedFoodLogEntry``, ``updateFoodLogEntry``,
    ``deleteFoodLogEntry``) ships the bytes in reverse order on the wire,
    the server fails the lookup, and the responses come back empty.
    """
    if isinstance(pk_field, list):
        return list(reversed([int(b) for b in pk_field]))
    if isinstance(pk_field, dict):
        # SimplePrimaryKey has f0 = byte[]
        inner = pk_field.get("f0")
        if isinstance(inner, list):
            return list(reversed([int(b) for b in inner]))
    return []


def _extract_search_results_from_decoded(
    decoded: Any,
    user_name: str = "",
) -> list[FoodSearchResult]:
    """Walk the decoded LoseItRemoteServiceResponse for SearchResultFood rows.

    The response wraps a ``SearchResults`` whose ``f0`` is an
    ``ArrayList`` of ``SearchResult`` polymorphic items. We keep only the
    ``SearchResultFood`` variants — headers (``All Foods`` / ``Previous
    Meals``) and meal-recall rows are intentionally filtered out.

    Field positions on SearchResultFood (verified against the schema and
    live data — order matches the deserializer in the JS bundle):

    - ``f0`` → primary key (SimplePrimaryKey)
    - ``f1`` → category   (e.g. "Pancakes", "Beverages")
    - ``f2`` → locale     (typically "en-US"; ignored)
    - ``f3`` → food name  (the human-friendly label)
    - ``f4`` → brand      (manufacturer / generic-by-brand)
    - ``f5`` → verification status (enum; ignored at this layer)
    - ``f6`` → flag (always observed as 1; ignored)
    """
    out: list[FoodSearchResult] = []
    for sr in _walk(decoded, fqcn=_SEARCH_RESULTS):
        items_holder = sr.get("f0")
        if not isinstance(items_holder, dict):
            continue
        items = items_holder.get("items", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("__type__") != _SEARCH_RESULT_FOOD:
                continue
            pk_bytes = _extract_pk_bytes(item.get("f0"))
            name = item.get("f3") or ""
            brand = item.get("f4") or ""
            category = item.get("f1") or ""
            # Lose It! stamps personal-DB foods with the user's email
            # local-part in the brand slot. Drop it so the brand column
            # doesn't surface the username in CLI output.
            if user_name:
                local_part = user_name.split("@", 1)[0]
                if brand in (user_name, local_part):
                    brand = ""
            if name and len(pk_bytes) == 16:
                out.append(
                    FoodSearchResult(
                        name=name,
                        brand=brand,
                        category=category,
                        pk_bytes=pk_bytes,
                    )
                )
        # First SearchResults wins (responses occasionally carry more).
        break
    return out


def search(http: HttpClient, query: str) -> list[FoodSearchResult]:
    """Search the LoseIt food database. Returns up to ~15 results."""
    logger.info("foods.search: query={q!r}", q=query)
    text = http.post_rpc(_build_search_payload(http.config, query))
    decoded = decode_response(text)
    results = _extract_search_results_from_decoded(decoded, user_name=http.config.user_name)
    logger.debug("foods.search: {n} results", n=len(results))
    if not results:
        logger.warning("foods.search: 0 results for query={q!r}", q=query)
    return results


# ── getUnsavedFoodLogEntry ──────────────────────────────────────────────────


def _build_unsaved_payload(
    config: Config,
    food: FoodSearchResult,
    locale: str = "en-US",
) -> str:
    if len(food.pk_bytes) != 16:
        raise ValueError("food.pk_bytes must be 16 bytes")

    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getUnsavedFoodLogEntry",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.interfaces.IPrimaryKey",
        "java.lang.String/2004016611",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",
        "[B/3308590456",
        locale,
        food.name,
    ]
    data: list[str] = ["1", "2", "3", "4", "4", "5", "6", "7", "7"]
    data += ["5", "0", "8", config.user_id, "9", str(config.hours_from_gmt)]
    data += ["10", "11", "16"]
    data += [str(int(b)) for b in reversed(food.pk_bytes)]
    data += ["12", "13"]
    return build_envelope(strings, data)


def _extract_unsaved_from_decoded(
    decoded: Any,
    user_name: str = "",
) -> UnsavedFoodLogEntry:
    """Pull the FoodLogEntry template + identifier + nutrients out of a decoded response.

    The unsaved-entry response wraps a ``FoodLogEntry`` whose fields
    (verified against live fixtures) are:

        f0 → FoodIdentifier   (name/brand/category/food PK)
        f1 → FoodLogEntryContext
        f2 → FoodServing      (nutrients + serving size + measure)
        f3 → bool
        f4 → long
        f5 → long
        f6 → SimplePrimaryKey (entry-PK placeholder)

    The ``getUnsavedFoodLogEntry`` response is wrapped in a
    ``LoseItRemoteServiceResponse`` like every other RPC; we walk into
    it to find the inner FoodLogEntry by FQCN.
    """
    out = UnsavedFoodLogEntry(
        name="",
        brand="",
        category="",
        food_pk_bytes=None,
        day_key="",
    )

    fle = next(_walk(decoded, fqcn=_UNSAVED_FOOD_LOG_ENTRY), None)
    if fle is None:
        return out

    # FoodIdentifier — name / brand / category / food PK.
    identifier = fle.get("f0") if isinstance(fle.get("f0"), dict) else None
    if identifier is not None:
        # FoodIdentifier schema (verified against live fixtures):
        #   f0 → raw int (-1)
        #   f1 → category
        #   f2 → locale (ignored)
        #   f3 → name
        #   f4 → brand
        #   f5 → ProductType enum
        #   f6 → raw int
        #   f7 → Verification enum
        #   f8 → long (last-modified epoch ms)
        #   f9 → PrimaryKey
        out.category = identifier.get("f1") or ""
        out.name = identifier.get("f3") or ""
        out.brand = identifier.get("f4") or ""
        pk_obj = identifier.get("f9")
        if isinstance(pk_obj, dict) and pk_obj.get("__type__") == _SIMPLE_PK:
            out.food_pk_bytes = _extract_pk_bytes(pk_obj)

    # FoodServing.f0 = FoodNutrients (HashMap inside); f1 = FoodServingSize.
    food_serving = fle.get("f2") if isinstance(fle.get("f2"), dict) else None
    if food_serving is not None:
        # Nutrients live in the HashMap under FoodNutrients.f0 (the
        # generated schema flattens it into a `java.util.HashMap` entry).
        for hm in _walk(food_serving, fqcn_prefix="java.util.HashMap"):
            entries_list = hm.get("entries")
            if not isinstance(entries_list, list):
                continue
            for key, val in entries_list:
                if (
                    isinstance(key, dict)
                    and isinstance(val, (int, float))
                    and "ordinal" in key
                    and isinstance(key["ordinal"], (int, float))
                    and 0 <= int(key["ordinal"]) <= 30
                ):
                    out.nutrients[int(key["ordinal"])] = float(val)

        # FoodServingSize sits at f1 of FoodServing.
        #
        # Schema [DOUBLE, BOOLEAN, OBJECT(FoodMeasure), DOUBLE, DOUBLE, DOUBLE].
        # f0 → suggested-log canonical_servings
        # f1 → isPrimary
        # f2 → FoodMeasure (carries the unit ordinal + the unit label)
        # f3 → food's per-serving canonical denominator (usually 1.0)
        # f4 → food's per-serving raw quantity in native unit  ← the key field
        # f5 → suggested-log raw quantity in native unit
        serving_size = food_serving.get("f1")
        if isinstance(serving_size, dict):
            qty = serving_size.get("f0")
            if isinstance(qty, (int, float)):
                out.serving_qty = float(qty)
            measure = serving_size.get("f2")
            if isinstance(measure, dict):
                ord_ = measure.get("ordinal")
                if isinstance(ord_, (int, float)):
                    out.food_measure_ordinal = int(ord_)
                unit = measure.get("unit")
                if isinstance(unit, str):
                    out.food_measure_unit = unit
            f3 = serving_size.get("f3")
            f4 = serving_size.get("f4")
            if isinstance(f3, (int, float)):
                out.canonical_per_serving = float(f3)
            if isinstance(f4, (int, float)):
                out.native_qty_per_serving = float(f4)

    # day_key: scan all strings in the FLE subtree for a GWT short-key.
    # FoodLogEntryContext carries it as a polymorphic field whose exact
    # position varies, so a tree-walk is the most robust path.
    def _scan_for_daykey(o: Any) -> str | None:
        if isinstance(o, str) and len(o) >= 5 and o.startswith("Zw") and o != "P__________":
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = _scan_for_daykey(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = _scan_for_daykey(v)
                if r:
                    return r
        return None

    daykey = _scan_for_daykey(fle)
    if daykey:
        out.day_key = daykey

    if user_name:
        local_part = user_name.split("@", 1)[0]
        if out.brand in {user_name, local_part}:
            out.brand = ""

    return out


# ── getFood ─────────────────────────────────────────────────────────────────


def _build_get_food_payload(config: Config, pk_bytes: list[int]) -> str:
    """Build the ``getFood`` GWT-RPC payload for a 16-byte food PK.

    Wire shape (observed 2026-06-11): UserId envelope + a SimplePrimaryKey
    wrapping the 16-byte food PK. No name, no locale. The PK bytes are
    reversed on the wire (same convention as ``_build_unsaved_payload``).
    """
    if len(pk_bytes) != 16:
        raise ValueError("food.pk_bytes must be 16 bytes")
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getFood",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.interfaces.IPrimaryKey",
        "java.lang.String/2004016611",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",
        "[B/3308590456",
    ]
    data: list[str] = ["1", "2", "3", "4", "3", "5", "6", "7"]
    data += ["5", "0", "8", config.user_id, "9", str(config.hours_from_gmt)]
    data += ["10", "11", "16"]
    data += [str(int(b)) for b in reversed(pk_bytes)]
    data += ["0"]
    return build_envelope(strings, data)


def get_food(http: HttpClient, pk_bytes: list[int]) -> FoodSearchResult:
    """Look up a food by its PK.

    Returns a :class:`FoodSearchResult` whose ``name``/``brand``/``category``
    come from the response's ``FoodIdentifier`` and whose ``pk_bytes`` is
    the caller-supplied PK (the server echoes the same PK back; we use the
    input to avoid an extra ``_extract_pk_bytes`` round-trip).

    The result can be handed straight to :func:`get_unsaved_food_log_entry`.
    """
    logger.info("foods.get_food: pk={h}", h=pk_to_hex(pk_bytes))
    text = http.post_rpc(_build_get_food_payload(http.config, pk_bytes))
    decoded = decode_response(text)
    identifier = next(_walk(decoded, fqcn=_FOOD_IDENTIFIER), None)
    if identifier is None:
        raise LoseItError(f"Food with id {pk_to_hex(pk_bytes)} not found")
    name = identifier.get("f3") or ""
    brand = identifier.get("f4") or ""
    category = identifier.get("f1") or ""
    if not name:
        raise LoseItError(f"Food with id {pk_to_hex(pk_bytes)} not found")
    # Drop the email-local-part placeholder Lose It! stamps on personal-DB
    # entries (mirrors the same scrub in _extract_search_results_from_decoded).
    if http.config.user_name:
        local_part = http.config.user_name.split("@", 1)[0]
        if brand in (http.config.user_name, local_part):
            brand = ""
    return FoodSearchResult(
        name=name,
        brand=brand,
        category=category,
        pk_bytes=list(pk_bytes),
    )


def get_unsaved_food_log_entry(
    http: HttpClient,
    food: FoodSearchResult,
) -> UnsavedFoodLogEntry:
    """Return the food's nutrient + serving template (no diary write)."""
    logger.info(
        "foods.get_unsaved_food_log_entry: name={n!r} brand={b!r} category={c!r}",
        n=food.name,
        b=food.brand,
        c=food.category,
    )
    text = http.post_rpc(_build_unsaved_payload(http.config, food))
    decoded = decode_response(text)
    unsaved = _extract_unsaved_from_decoded(decoded, user_name=http.config.user_name)
    logger.debug(
        "foods.get_unsaved_food_log_entry: measure_ord={mo} serving_qty={sq} "
        "n_nutrients={nn} day_key={dk!r}",
        mo=unsaved.food_measure_ordinal,
        sq=unsaved.serving_qty,
        nn=len(unsaved.nutrients or {}),
        dk=unsaved.day_key,
    )
    return unsaved
