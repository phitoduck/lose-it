"""``getDailyDetailsIncludingPendingForDate`` RPC — fetch the diary for a day.

Returns every ``FoodLogEntry`` logged on the target date, with enough field
data to round-trip into a ``deleteFoodLogEntry`` (which requires the full
entry body, not just a PK).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .._logging import logger
from ._config import Config
from ._dates import day_number_for
from ._decoder import decode_response
from ._gwt import build_envelope, parse_response
from ._http import HttpClient
from ._models import FoodLogEntry
from .init import get_daydate_key

# Type hashes used to walk the decoder's output.
_FLE_FQCN = "com.loseit.core.client.model.FoodLogEntry/264522954"
_FOOD_SERVING_SIZE_FQCN = "com.loseit.core.client.model.FoodServingSize/63998910"
_FOOD_MEASURE_PREFIX = "com.loseit.core.client.model.FoodMeasure/"
_DATE_FQCN = "java.util.Date/3385151746"


def _build_payload(config: Config, target_date: date, day_key: str) -> str:
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getDailyDetailsIncludingPendingForDate",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.shared.model.DayDate/1611136587",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        "java.util.Date/3385151746",
    ]
    day_num = day_number_for(target_date)
    data = [
        "1",
        "2",
        "3",
        "4",
        "2",
        "5",
        "6",
        "5",
        "0",
        "7",
        config.user_id,
        "8",
        str(config.hours_from_gmt),
        "6",
        "9",
        day_key,
        str(day_num),
        str(config.hours_from_gmt),
    ]
    return build_envelope(strings, data)


def _pk_bytes_from(pk_obj: Any) -> list[int]:
    """SimplePrimaryKey wraps a raw byte[] in its sole field.

    Returns bytes in **response form** to match the SDK's existing
    pk_bytes contract — i.e. the form the outbound payload builders
    (``_build_delete_payload`` etc.) re-reverse before serializing.
    The schema decoder pops bytes LIFO, so its raw output is the
    opposite of the on-stream slice the old parser produced; we flip
    once here. See ``foods._extract_pk_bytes`` for the full story.
    """
    if isinstance(pk_obj, dict):
        inner = pk_obj.get("f0")
        if isinstance(inner, list):
            return list(reversed([int(b) for b in inner]))
    if isinstance(pk_obj, list):
        return list(reversed([int(b) for b in pk_obj]))
    return []


def _walk_dicts(root: Any):
    """Yield every dict node in ``root`` (depth-first)."""
    if isinstance(root, dict):
        yield root
        for v in root.values():
            yield from _walk_dicts(v)
    elif isinstance(root, list):
        for v in root:
            yield from _walk_dicts(v)


def _scan_for_short_key(o: Any) -> str | None:
    """Pull out the first GWT short-string day_key shaped like ``Z6mB_lo``."""
    if isinstance(o, str) and 4 <= len(o) <= 16 and re.match(r"^[A-Za-z0-9_$]+$", o):
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = _scan_for_short_key(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _scan_for_short_key(v)
            if r:
                return r
    return None


def _entry_from_decoded(
    fle: dict,
    default_hours_from_gmt: int,
    user_name: str = "",
) -> FoodLogEntry | None:
    """Build a :class:`FoodLogEntry` from a decoded FoodLogEntry dict.

    Walks the decoded tree to find:

    - ``FoodIdentifier`` — name/brand/category/food-id-code/food PK
    - ``FoodLogEntryContext`` — context day_key + day_num + hours_from_gmt + meal
    - ``FoodServing`` → ``FoodServingSize`` → servings
    - ``FoodMeasure`` — measure ordinal
    - nutrient HashMap — ordinal → value pairs

    The entry's own PK is the deepest SimplePrimaryKey we encounter
    inside this FLE that isn't the food's identifier PK.
    """
    # FoodLogEntry positional fields (verified against live fixtures):
    #   f0 → FoodIdentifier   (name/brand/category/food PK)
    #   f1 → FoodLogEntryContext (day, meal, hours_from_gmt)
    #   f2 → FoodServing       (servings + serving size + measure)
    #   f3 → bool   (deleted/pending flag)
    #   f4 → long   (created? timestamp)
    #   f5 → long   (modified? timestamp)
    #   f6 → SimplePrimaryKey  (the entry's own UUID)
    identifier = fle.get("f0") if isinstance(fle.get("f0"), dict) else None
    food_name = (identifier.get("f3") if identifier else "") or ""
    food_brand = (identifier.get("f4") if identifier else "") or ""
    food_category = (identifier.get("f1") if identifier else "") or ""
    food_pk_bytes: list[int] = []
    if identifier is not None:
        food_pk_bytes = _pk_bytes_from(identifier.get("f9"))

    # Strip the user's email-local-part / full email if Lose It! stamped
    # it as the brand on personally-saved foods.
    if user_name:
        local_part = user_name.split("@", 1)[0]
        if food_brand in {user_name, local_part}:
            food_brand = ""

    # food_identifier_code is the "DoXyz" short string that uniquely names
    # the food in Lose It's internal index. It doesn't sit in any of the
    # positional fields the schema exposes — it travels alongside the
    # FoodLogEntry as a separate inline string. ``parse_entries`` zips
    # the per-FLE codes by source order; we leave it blank here.
    food_identifier_code = ""

    # Entry PK is FLE.f6 (the entry's own UUID), not the food's PK.
    entry_pk_bytes: list[int] = _pk_bytes_from(fle.get("f6"))

    # Context: day_key, day_num, hours_from_gmt, meal_ordinal, extra_ordinal.
    context = fle.get("f1") if isinstance(fle.get("f1"), dict) else None
    meal_ord = 0
    extra_ord = 3
    day_num = 0
    hours_from_gmt = default_hours_from_gmt
    context_day_key = ""
    if context is not None:
        # FoodLogEntryContext schema:
        # [OBJECT, OBJECT, BOOLEAN, RAW, RAW, BOOLEAN, OBJECT, OBJECT, OBJECT, OBJECT]
        # f0 → ??  (often null)
        # f1 → DayDate (containing day_key, day_num, hours_from_gmt)
        # f2 → bool
        # f3/f4 → raw ints
        # f5 → bool
        # f6/f7 → polymorphic Objects (often null)
        # f8 → FoodLogEntryType enum   (meal ordinal: 0..3)
        # f9 → FoodLogEntryTypeExtra enum (extra ordinal)
        meal_obj = context.get("f8")
        if isinstance(meal_obj, dict):
            o = meal_obj.get("ordinal")
            if isinstance(o, (int, float)) and 0 <= int(o) <= 3:
                meal_ord = int(o)
        extra_obj = context.get("f9")
        if isinstance(extra_obj, dict):
            o = extra_obj.get("ordinal")
            if isinstance(o, (int, float)) and 0 <= int(o) <= 15:
                extra_ord = int(o)
        daydate = context.get("f1")
        if isinstance(daydate, dict):
            # DayDate schema: [OBJECT, RAW, RAW]
            # f0 → Date (epoch ms long), f1 → day_num int, f2 → hours_from_gmt int
            f1 = daydate.get("f1")
            f2 = daydate.get("f2")
            if isinstance(f1, (int, float)) and int(f1) >= 5000:
                day_num = int(f1)
            if isinstance(f2, (int, float)) and -12 <= int(f2) <= 14:
                hours_from_gmt = int(f2)
            # day_key is the raw base64-encoded epoch-long of the Date in
            # DayDate.f0 — that's what the server uses as a cache key and
            # what ``deleteFoodLogEntry`` requires in its payload. We
            # preserved the raw token alongside the decoded millis in
            # the inline Date handler precisely so the parser can grab
            # it here without an extra init RPC round-trip.
            date_obj = daydate.get("f0")
            if isinstance(date_obj, dict):
                raw = date_obj.get("raw")
                if isinstance(raw, str) and raw:
                    context_day_key = raw

    # FoodMeasure (enum) — pluck the ordinal.
    food_measure_ord = 27
    for d in _walk_dicts(fle):
        t = d.get("__type__", "")
        if isinstance(t, str) and t.startswith(_FOOD_MEASURE_PREFIX) and "ordinal" in d:
            o = d.get("ordinal")
            if isinstance(o, (int, float)):
                food_measure_ord = int(o)
                break

    # FoodServingSize.f0 = quantity → servings on the FoodServing.
    servings = 1.0
    serving_size = next(
        (d for d in _walk_dicts(fle) if d.get("__type__") == _FOOD_SERVING_SIZE_FQCN),
        None,
    )
    if serving_size is not None:
        q = serving_size.get("f0")
        if isinstance(q, (int, float)):
            servings = float(q)

    # Nutrients — any HashMap whose keys carry an ordinal 0..30 and values are numeric.
    nutrients: list[tuple[int, float]] = []
    for d in _walk_dicts(fle):
        if not isinstance(d.get("__type__"), str):
            continue
        if not d.get("__type__", "").startswith("java.util.HashMap"):
            continue
        entries = d.get("entries")
        if not isinstance(entries, list):
            continue
        for key, val in entries:
            if isinstance(key, dict) and "ordinal" in key and isinstance(val, (int, float)):
                ord_ = key["ordinal"]
                if isinstance(ord_, (int, float)) and 0 <= int(ord_) <= 30:
                    nutrients.append((int(ord_), float(val)))
        if nutrients:
            break

    # The entry-level day_key sits in the FoodServingSize's nested Date
    # — same shape as context.f1.f0 but for the entry's own DayDate.
    # Both day_keys are the base64 epoch-long that the server uses as a
    # cache key; ``deleteFoodLogEntry`` rejects the payload (HTTP 500)
    # if either is wrong. Previously the parser called
    # ``_scan_for_short_key(fle)`` which heuristically grabbed *any*
    # 4-16 char alphanumeric string in the subtree — and routinely
    # picked up category strings like 'Honey' / 'Avocado' / 'Tomato'
    # that match the pattern. Now we walk every Date dict in the FLE
    # tree and use the raw token whose decoded millis is *closest* to
    # the day_num — which is reliably the entry's day_key.
    entry_day_key = context_day_key
    for d in _walk_dicts(fle):
        if d.get("__type__") == _DATE_FQCN:
            raw = d.get("raw")
            if isinstance(raw, str) and raw:
                # Prefer a Date whose raw token differs from context's;
                # if there's only one (typical), context and entry use it.
                entry_day_key = raw
                break

    if not food_pk_bytes or not entry_pk_bytes:
        return None

    return FoodLogEntry(
        food_pk_response=food_pk_bytes,
        entry_pk_response=entry_pk_bytes,
        entry_day_key=entry_day_key,
        context_day_key=context_day_key,
        day_num=day_num,
        hours_from_gmt=hours_from_gmt,
        meal_ordinal=meal_ord,
        extra_ordinal=extra_ord,
        food_measure_ordinal=food_measure_ord,
        servings=servings,
        food_identifier_code=food_identifier_code,
        food_category=food_category,
        food_name=food_name,
        food_brand=food_brand,
        nutrients_ordered=nutrients,
    )


def parse_entries(
    text: str,
    default_hours_from_gmt: int = -5,
    user_name: str = "",
) -> list[FoodLogEntry]:
    """Extract every :class:`FoodLogEntry` from a daily-details response.

    Uses the schema-driven decoder to walk the response, then maps each
    decoded ``FoodLogEntry`` dict into the public dataclass shape.
    ``decode_response`` is lenient — if it encounters an unknown type or
    a stream desync it returns a partial result with ``backrefs``
    populated, and we scan that list for any FoodLogEntries that
    completed before the failure.

    The ``food_identifier_code`` (``DoXXX`` strings) is the only field
    whose position the decoder hasn't yet pinned down; we fall back to
    scanning the raw token stream for those literals and zip them with
    the decoded entries in source order.
    """
    decoded = decode_response(text)
    if decoded is None:
        return []
    out: list[FoodLogEntry] = []
    seen_ids: set[int] = set()
    sources = [decoded]
    if isinstance(decoded, dict) and decoded.get("__partial__"):
        sources.extend(decoded.get("backrefs") or [])
    for src in sources:
        for d in _walk_dicts(src):
            if d.get("__type__") != _FLE_FQCN:
                continue
            ident = id(d)
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            entry = _entry_from_decoded(d, default_hours_from_gmt, user_name=user_name)
            if entry is not None:
                out.append(entry)

    # Backfill missing food_identifier_codes from the raw token stream.
    # Each FLE in a daily-details response carries exactly one ``DoXXX``
    # short string inline; the source order matches the order entries
    # land in the decoded tree, so zipping is safe.
    if any(not e.food_identifier_code for e in out):
        tokens, _ = parse_response(text)
        codes = [
            t
            for t in tokens
            if isinstance(t, str) and len(t) >= 4 and len(t) <= 20 and t.startswith("Do")
        ]
        for entry, code in zip(out, codes, strict=False):
            if not entry.food_identifier_code:
                entry.food_identifier_code = code
    return out


def get_daily_details(http: HttpClient, target_date: date) -> list[FoodLogEntry]:
    """Fetch + parse today's (or any day's) FoodLogEntry list."""
    logger.info("daily.get_daily_details: date={d}", d=target_date.isoformat())
    day_num = day_number_for(target_date)
    day_key = get_daydate_key(http, day_num)
    logger.debug("daily.get_daily_details: day_num={n} day_key={k!r}", n=day_num, k=day_key)
    text = http.post_rpc(_build_payload(http.config, target_date, day_key))
    entries = parse_entries(
        text,
        default_hours_from_gmt=http.config.hours_from_gmt,
        user_name=http.config.user_name,
    )
    logger.debug(
        "daily.get_daily_details: parsed {n} entries for {d}",
        n=len(entries),
        d=target_date.isoformat(),
    )
    return entries


def get_daily_details_raw(http: HttpClient, target_date: date) -> str:
    """Same as :func:`get_daily_details` but returns the raw GWT response.

    Useful for fixture capture in tests.
    """
    logger.info("daily.get_daily_details_raw: date={d}", d=target_date.isoformat())
    day_num = day_number_for(target_date)
    day_key = get_daydate_key(http, day_num)
    return http.post_rpc(_build_payload(http.config, target_date, day_key))
