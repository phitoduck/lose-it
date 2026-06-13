"""``getDailyDetailsIncludingPendingForDate`` RPC — fetch the diary for a day.

Returns every ``FoodLogEntry`` logged on the target date, with enough field
data to round-trip into a ``deleteFoodLogEntry`` (which requires the full
entry body, not just a PK).

Also exposes the bulk variant
:func:`get_daily_details_range` which wraps the
``getDailyDetailsIncludingPendingForDateRange`` RPC. The range RPC returns
one ``DailyDetails`` block per day in ``[start, end]`` inclusive, in the
same wire shape as the per-day version — we decode the response into a
``{date: [FoodLogEntry]}`` map so callers don't have to know about day
numbers downstream.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from .._logging import logger
from ..models import FoodLogEntry
from ._config import Config
from ._dates import day_number_for
from ._decoder import decode_response
from ._gwt import build_envelope, parse_response
from ._http import HttpClient, LoseItError
from .init import _FALLBACK_DAY_KEY, get_daydate_key

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


def _epoch_ms_to_utc(ms: int | float) -> datetime:
    """GWT long epoch-ms -> aware datetime in UTC.

    Lose It! stores wall-clock UTC server-side; we surface that exact
    timezone (offset 0) so callers don't have to think about local-time
    conversions for backup/restore identity. See spec §4.4 — the upsert
    join key compares timestamps with a ±10-minute window, which is
    only meaningful if both sides agree on the zone.
    """
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


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

    # FLE.f4 / FLE.f5 — server-side audit timestamps as epoch-ms longs.
    # f4 is the original log time ("created"), f5 the last edit time
    # ("modified"). Both are written as GWT base64-longs in the wire
    # stream and surface as Python ``int`` after the LONG decoder runs;
    # we guard on positivity so a sentinel 0 (rare, but possible on
    # partially-synthesized server payloads) decodes to ``None`` rather
    # than the 1970 epoch.
    f4 = fle.get("f4")
    f5 = fle.get("f5")
    created_at = _epoch_ms_to_utc(f4) if isinstance(f4, (int, float)) and f4 > 0 else None
    modified_at = _epoch_ms_to_utc(f5) if isinstance(f5, (int, float)) and f5 > 0 else None

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
        created_at=created_at,
        modified_at=modified_at,
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


# ── Bulk range fetch (T0) ────────────────────────────────────────────────────


class TooMuchData(Exception):
    """Raised when a range RPC fails with an oversize / rate-limit / 5xx shape.

    The fetch primitive that lives one layer up (T2 in the backup spec)
    catches this and recurses into a smaller date grain. Concretely we
    map HTTP 413 (request entity too large), 429 (rate limited), and
    any 5xx (server side failure) responses to this exception. GWT-RPC
    ``//EX[…]`` envelopes whose error text mentions ``oversize`` /
    ``too large`` / ``size`` are also mapped here — the server has been
    observed to dress up oversize replies as a GWT error rather than
    raw HTTP 413 depending on which layer in their stack tripped.
    """


# Sentinels lifted out of the function so tests can monkey-patch them
# without reaching into the function body.
_RANGE_RPC_RETRYABLE_STATUSES = frozenset({413, 429})


def _is_oversize_gwt_error(text: str) -> bool:
    """Detect the //EX error shape the range RPC emits on oversize windows."""
    if not text.startswith("//EX"):
        return False
    lower = text.lower()
    return any(needle in lower for needle in ("oversize", "too large", "size limit"))


def build_range_payload(
    config: Config,
    start_day_num: int,
    start_day_key: str,
    end_day_num: int,
    end_day_key: str,
) -> str:
    """Build the ``getDailyDetailsIncludingPendingForDateRange`` GWT-RPC envelope.

    The wire shape (verified against the captured fixture) is::

        strings:
            base_url, policy_hash,
            LoseItRemoteService, getDailyDetailsIncludingPendingForDateRange,
            ServiceRequestToken, Integer, DayDate, UserId,
            user_name, Date

        data:
            url, policy, service, method,           # ref headers
            4,                                       # arg_count
            ServiceRequestToken, Integer, DayDate, DayDate,    # arg type refs
            ServiceRequestToken, 0, UserId, user_id, user_name, hours,
            Integer, user_id,                       # second arg: integer (user_id again)
            DayDate, Date, start_day_key, start_day_num, hours,
            DayDate, Date, end_day_key,   end_day_num,   hours

    The four arguments are: token, an integer (the server uses this as a
    redundant userId int — both call sites in the wire match), the start
    DayDate, the end DayDate.
    """
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getDailyDetailsIncludingPendingForDateRange",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "java.lang.Integer/3438268394",
        "com.loseit.core.shared.model.DayDate/1611136587",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
        "java.util.Date/3385151746",
    ]
    data = [
        "1",
        "2",
        "3",
        "4",
        "4",
        "5",
        "6",
        "7",
        "7",
        "5",
        "0",
        "8",
        config.user_id,
        "9",
        str(config.hours_from_gmt),
        "6",
        config.user_id,
        "7",
        "10",
        start_day_key,
        str(start_day_num),
        str(config.hours_from_gmt),
        "7",
        "10",
        end_day_key,
        str(end_day_num),
        str(config.hours_from_gmt),
    ]
    return build_envelope(strings, data)


def parse_entries_by_day(
    text: str,
    default_hours_from_gmt: int = -5,
    user_name: str = "",
) -> dict[int, list[FoodLogEntry]]:
    """Decode a range-RPC response into a ``{day_num: [FoodLogEntry]}`` map.

    The response wraps a ``DailyDetails[]`` array — one block per day in
    the requested range, in order. Each block's day_num lives in its
    ``DailyLogEntry.context.f1`` DayDate slot; FoodLogEntries hang off
    the same ``DailyLogEntry`` subtree. We walk the array, find day_num
    per block, and call :func:`_entry_from_decoded` on every FLE in the
    subtree.

    Days with zero entries get an explicit empty list — callers
    distinguish "fetched, none logged" from "not fetched" purely by
    presence of the key.

    Entries whose ``food_identifier_code`` is blank get backfilled from
    the raw token stream in source order, the same way the single-day
    :func:`parse_entries` does it.
    """
    decoded = decode_response(text)
    if decoded is None:
        return {}

    # Find the DailyDetails array. The clean shape is
    # LoseItRemoteServiceResponse.f3.items; on a decoder partial we
    # fall back to scanning backrefs for any [LDailyDetails;.
    array_holder: dict | None = None
    if isinstance(decoded, dict) and decoded.get("__partial__"):
        for ref in decoded.get("backrefs") or []:
            if (
                isinstance(ref, dict)
                and isinstance(ref.get("__type__"), str)
                and ref["__type__"].startswith("[Lcom.loseit.core.client.model.DailyDetails;")
            ):
                array_holder = ref
                break
    elif isinstance(decoded, dict):
        candidate = decoded.get("f3")
        if (
            isinstance(candidate, dict)
            and isinstance(candidate.get("__type__"), str)
            and candidate["__type__"].startswith("[Lcom.loseit.core.client.model.DailyDetails;")
        ):
            array_holder = candidate

    if array_holder is None:
        return {}

    out: dict[int, list[FoodLogEntry]] = {}
    seen_global: set[int] = set()

    for dd_block in array_holder.get("items") or []:
        if not isinstance(dd_block, dict):
            continue
        day_num = _extract_day_num(dd_block)
        if day_num is None:
            continue
        block_entries: list[FoodLogEntry] = []
        for d in _walk_dicts(dd_block):
            if d.get("__type__") != _FLE_FQCN:
                continue
            ident = id(d)
            if ident in seen_global:
                continue
            seen_global.add(ident)
            entry = _entry_from_decoded(d, default_hours_from_gmt, user_name=user_name)
            if entry is not None:
                block_entries.append(entry)
        # Days with no entries still get an empty-list slot so callers
        # can distinguish "checked, nothing logged" from "skipped".
        out[day_num] = block_entries

    # Backfill missing food_identifier_codes from the raw token stream in
    # source order — same approach as parse_entries, except we have to
    # iterate the days in the wire's natural order (the order they
    # appeared in the DailyDetails array, which is also the iteration
    # order of ``out`` since Python 3.7 preserves insertion order).
    flat_entries = [e for entries in out.values() for e in entries]
    if any(not e.food_identifier_code for e in flat_entries):
        tokens, _ = parse_response(text)
        codes = [
            t for t in tokens if isinstance(t, str) and 4 <= len(t) <= 20 and t.startswith("Do")
        ]
        for entry, code in zip(flat_entries, codes, strict=False):
            if not entry.food_identifier_code:
                entry.food_identifier_code = code
    return out


def _extract_day_num(dd_block: dict) -> int | None:
    """Find the day_num for a single ``DailyDetails`` block.

    The reliable spot is ``DailyLogEntry.f1`` — a DayDate. We try that
    first, then fall back to scanning every DayDate dict in the block
    (a no-FLE block may not have a DailyLogEntry at all).
    """
    log_entry = dd_block.get("f3")
    if isinstance(log_entry, dict):
        for v in log_entry.values():
            if (
                isinstance(v, dict)
                and isinstance(v.get("__type__"), str)
                and v["__type__"].startswith("com.loseit.core.shared.model.DayDate")
            ):
                day_num_val = v.get("f1")
                if isinstance(day_num_val, (int, float)):
                    return int(day_num_val)
    # Fallback: scan the whole block.
    for d in _walk_dicts(dd_block):
        if isinstance(d.get("__type__"), str) and d["__type__"].startswith(
            "com.loseit.core.shared.model.DayDate"
        ):
            v = d.get("f1")
            if isinstance(v, (int, float)):
                return int(v)
    return None


def get_daily_details_range(
    http: HttpClient,
    start: date,
    end: date,
    *,
    day_keys: dict[int, str] | None = None,
) -> dict[date, list[FoodLogEntry]]:
    """Bulk diary fetch via ``getDailyDetailsIncludingPendingForDateRange``.

    Returns a ``{date: [FoodLogEntry]}`` map covering every day in the
    inclusive range ``[start, end]``. Days with no entries are present
    with an empty list — callers should treat absence of a key as
    "server omitted this day" (shouldn't happen in practice).

    ``day_keys`` is an optional pre-cached ``{day_num: day_key}`` window
    (typically the one ``get_init`` populates). The server only really
    cares about ``day_num`` — the day_key string is treated as a cache
    key — so for day_nums outside the cache we send the
    ``_FALLBACK_DAY_KEY`` (``"ZZZZZZZ"``) placeholder which the server
    accepts unconditionally.

    Raises :class:`TooMuchData` on HTTP 413 / 429 / 5xx responses or any
    GWT ``//EX`` envelope shaped like an oversize / size-limit error;
    callers (T2) catch this and bisect into a smaller grain.
    """
    if end < start:
        raise ValueError(f"end {end!r} is before start {start!r}")

    logger.info(
        "daily.get_daily_details_range: start={s} end={e}",
        s=start.isoformat(),
        e=end.isoformat(),
    )

    start_day_num = day_number_for(start)
    end_day_num = day_number_for(end)
    keys = day_keys or {}
    start_key = keys.get(start_day_num, _FALLBACK_DAY_KEY)
    end_key = keys.get(end_day_num, _FALLBACK_DAY_KEY)

    payload = build_range_payload(
        http.config,
        start_day_num=start_day_num,
        start_day_key=start_key,
        end_day_num=end_day_num,
        end_day_key=end_key,
    )

    try:
        text = http.post_rpc(payload)
    except httpx.HTTPStatusError as exc:
        # The httpx layer raises on non-200 when ``raise_for_status``
        # is in play; the SDK's HttpClient currently raises LoseItError
        # instead, but keep this branch for defence in depth.
        status = exc.response.status_code
        if status in _RANGE_RPC_RETRYABLE_STATUSES or 500 <= status < 600:
            raise TooMuchData(
                f"range RPC failed with HTTP {status} — bisect into smaller grain"
            ) from exc
        raise
    except LoseItError as exc:
        # HttpClient maps non-200 + //EX to LoseItError; sniff the message
        # for oversize / 413 / 429 / 5xx markers.
        msg = str(exc)
        if _looks_like_too_much_data(msg):
            raise TooMuchData(f"range RPC failed (likely oversize/throttled): {msg}") from exc
        raise

    if _is_oversize_gwt_error(text):
        raise TooMuchData(f"range RPC returned oversize //EX envelope: {text[:200]}")

    by_day_num = parse_entries_by_day(
        text,
        default_hours_from_gmt=http.config.hours_from_gmt,
        user_name=http.config.user_name,
    )

    # Convert day_num → date and ensure every day in the inclusive
    # range is present, even if the server elided it (shouldn't happen,
    # but a missing day from a bulk fetch should still surface as an
    # empty list rather than KeyError downstream).
    result: dict[date, list[FoodLogEntry]] = {}
    for offset in range((end - start).days + 1):
        d = start + timedelta(days=offset)
        result[d] = by_day_num.get(start_day_num + offset, [])

    logger.debug(
        "daily.get_daily_details_range: {n_days} days, {n_entries} total entries",
        n_days=len(result),
        n_entries=sum(len(v) for v in result.values()),
    )
    return result


def _looks_like_too_much_data(message: str) -> bool:
    """Match the substrings HttpClient emits for 413 / 429 / 5xx / oversize."""
    lower = message.lower()
    if "http 413" in lower or "http 429" in lower:
        return True
    # 5xx — any "HTTP 5xx" prefix.
    m = re.search(r"http 5\d{2}", lower)
    if m is not None:
        return True
    return any(needle in lower for needle in ("oversize", "too large", "size limit"))
