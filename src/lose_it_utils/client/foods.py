"""Food lookup RPCs: ``searchFoods`` and ``getUnsavedFoodLogEntry``.

``searchFoods`` is the autocomplete-style endpoint: send a query, get back
candidate foods with their primary keys.

``getUnsavedFoodLogEntry`` is the second step before logging — it returns
the food's serving size + nutrient template, which the client then scales
by the desired number of servings when posting to ``updateFoodLogEntry``.
"""
from __future__ import annotations

import re

from ._config import Config
from ._gwt import build_envelope, parse_response, resolve_string
from ._http import HttpClient
from ._models import FoodSearchResult, UnsavedFoodLogEntry


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


def _extract_search_results(tokens: list, string_table: list[str]) -> list[FoodSearchResult]:
    """Extract food results from a ``searchFoods`` response.

    Anchors on the per-row delimiter
    ``[16 (length), [B_ref, SimplePrimaryKey_ref, SearchResultFood_ref]``
    (in reverse-of-write order, since GWT responses are reversed).
    """
    food_type_ref = pk_type_ref = bytes_type_ref = None
    for i, s in enumerate(string_table):
        ref = i + 1
        if "SearchResultFood/" in s:
            food_type_ref = ref
        elif "SimplePrimaryKey/" in s:
            pk_type_ref = ref
        elif s == "[B/3308590456":
            bytes_type_ref = ref
    if not (food_type_ref and pk_type_ref and bytes_type_ref):
        return []

    delim = [16, bytes_type_ref, pk_type_ref, food_type_ref]
    ends: list[int] = []
    for i in range(len(tokens) - 3):
        if tokens[i:i + 4] == delim:
            ends.append(i + 3)

    # First entry starts after the first negative-int marker (a GWT idiom).
    start = 0
    for i, t in enumerate(tokens[:80]):
        if isinstance(t, int) and t < 0:
            start = i + 1
            break

    foods: list[FoodSearchResult] = []
    prev = start
    skip = {"All Foods", "BB", "BQ", "en-US", "I", "Z"}
    for end in ends:
        chunk = tokens[prev:end + 1]
        pk_bytes: list[int] = []
        if len(chunk) >= 4 + 16:
            pk_bytes = [int(x) for x in chunk[-(4 + 16):-4]]

        candidates: list[str] = []
        for t in chunk:
            if isinstance(t, int) and 1 <= t <= len(string_table):
                s = resolve_string(string_table, t)
                if not s:
                    continue
                if s.startswith("com.") or s.startswith("java.") or s.startswith("["):
                    continue
                if s in skip:
                    continue
                candidates.append(s)

        name = brand = category = ""
        if candidates:
            name = max(candidates, key=len)
            for s in candidates:
                if len(s) <= 16 and s[0].isupper() and " " not in s and s.lower() != "rich":
                    category = s
                    break
            for s in candidates:
                if s != name and s != category and 0 < len(s) <= 30:
                    brand = s
                    break

        if name and pk_bytes and len(pk_bytes) == 16:
            foods.append(FoodSearchResult(
                name=name, brand=brand, category=category, pk_bytes=pk_bytes,
            ))
        prev = end + 1
    return foods


def search(http: HttpClient, query: str) -> list[FoodSearchResult]:
    """Search the LoseIt food database. Returns up to ~15 results."""
    text = http.post_rpc(_build_search_payload(http.config, query))
    tokens, strings = parse_response(text)
    return _extract_search_results(tokens, strings)


# ── getUnsavedFoodLogEntry ──────────────────────────────────────────────────

def _build_unsaved_payload(
    config: Config, food: FoodSearchResult, locale: str = "en-US",
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


def _parse_unsaved_response(tokens: list, string_table: list[str]) -> UnsavedFoodLogEntry:
    fm_ref = dbl_ref = bytes_ref = pk_ref = serving_size_ref = food_measure_ref = None
    for i, s in enumerate(string_table):
        ref = i + 1
        if "FoodMeasurement/" in s:
            fm_ref = ref
        elif s == "java.lang.Double/858496421":
            dbl_ref = ref
        elif s == "[B/3308590456":
            bytes_ref = ref
        elif "SimplePrimaryKey/" in s:
            pk_ref = ref
        elif "FoodServingSize/" in s:
            serving_size_ref = ref
        elif "FoodMeasure/" in s:
            food_measure_ref = ref

    out = UnsavedFoodLogEntry(
        name="", brand="", category="", food_pk_bytes=None, day_key="",
    )

    skip = {"en-US", "I", "Z", "All Foods", "P__________"}
    candidates = [
        s for s in string_table
        if s and not (s.startswith("com.") or s.startswith("java.") or s.startswith("["))
        and s not in skip
    ]
    if candidates:
        out.name = max(candidates, key=len)
        for s in candidates:
            if len(s) <= 20 and " " not in s and s and s[0].isupper():
                out.category = s
                break
        for s in candidates:
            if s and s != out.name and s != out.category and len(s) <= 30:
                out.brand = s
                break

    for t in tokens:
        if isinstance(t, str) and len(t) >= 5 and t.startswith("Zw") and t != "P__________":
            out.day_key = t
            break

    if bytes_ref and pk_ref:
        pk_positions = []
        for i in range(16, len(tokens) - 2):
            if (tokens[i] == 16 and tokens[i + 1] == bytes_ref and tokens[i + 2] == pk_ref):
                maybe = tokens[i - 16:i]
                if all(isinstance(x, (int, float)) for x in maybe):
                    pk_positions.append([int(x) for x in maybe])
        if len(pk_positions) >= 2:
            out.food_pk_bytes = pk_positions[1]
        elif pk_positions:
            out.food_pk_bytes = pk_positions[0]

    if fm_ref and dbl_ref:
        for i in range(len(tokens) - 3):
            if (tokens[i + 3] == fm_ref and tokens[i + 1] == dbl_ref
                    and isinstance(tokens[i + 2], int)
                    and isinstance(tokens[i], (int, float))):
                ord_ = int(tokens[i + 2])
                if 0 <= ord_ <= 30:
                    out.nutrients[ord_] = float(tokens[i])

    if serving_size_ref:
        for i in range(1, len(tokens)):
            if tokens[i] == serving_size_ref and isinstance(tokens[i - 1], (int, float)):
                out.serving_qty = float(tokens[i - 1])
                break
    if food_measure_ref:
        for i in range(1, len(tokens)):
            if tokens[i] == food_measure_ref and isinstance(tokens[i - 1], int):
                out.food_measure_ordinal = int(tokens[i - 1])
                break

    return out


def get_unsaved_food_log_entry(
    http: HttpClient, food: FoodSearchResult,
) -> UnsavedFoodLogEntry:
    """Return the food's nutrient + serving template (no diary write)."""
    text = http.post_rpc(_build_unsaved_payload(http.config, food))
    tokens, strings = parse_response(text)
    return _parse_unsaved_response(tokens, strings)
