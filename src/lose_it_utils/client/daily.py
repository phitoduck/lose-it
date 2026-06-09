"""``getDailyDetailsIncludingPendingForDate`` RPC — fetch the diary for a day.

Returns every ``FoodLogEntry`` logged on the target date, with enough field
data to round-trip into a ``deleteFoodLogEntry`` (which requires the full
entry body, not just a PK).
"""

from __future__ import annotations

import re
from datetime import date

from ._config import Config
from ._dates import day_number_for
from ._gwt import build_envelope, is_food_identifier_code, parse_response
from ._http import HttpClient
from ._models import FoodLogEntry
from .init import get_daydate_key


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


def _resolve_refs(string_table: list[str]) -> dict[str, int]:
    refs: dict[str, int] = {}
    for i, s in enumerate(string_table):
        ref = i + 1
        if s == "[B/3308590456":
            refs["bytes"] = ref
        elif s.startswith("com.loseit.core.client.model.SimplePrimaryKey/"):
            refs["pk"] = ref
        elif s.startswith("com.loseit.core.client.model.FoodLogEntry/"):
            refs["food_log_entry"] = ref
        elif s.startswith("com.loseit.core.client.model.interfaces.FoodLogEntryType/"):
            refs["meal"] = ref
        elif s.startswith("com.loseit.core.client.model.interfaces.FoodLogEntryTypeExtra/"):
            refs["extra"] = ref
        elif s.startswith("com.loseit.core.client.model.FoodMeasure/"):
            refs["food_measure"] = ref
        elif s.startswith("com.loseit.healthdata.model.shared.food.FoodMeasurement/"):
            refs["food_measurement"] = ref
        elif s == "java.lang.Double/858496421":
            refs["double"] = ref
    return refs


def _find_pk_blocks(tokens: list, bytes_ref: int, pk_ref: int) -> list[dict]:
    """Locate every 16-byte PK block followed by ``[16, bytes_ref, pk_ref, "<key>"]``."""
    blocks: list[dict] = []
    for i in range(16, len(tokens) - 3):
        if (
            tokens[i] == 16
            and tokens[i + 1] == bytes_ref
            and tokens[i + 2] == pk_ref
            and isinstance(tokens[i + 3], str)
        ):
            slice_ = tokens[i - 16 : i]
            if all(isinstance(x, (int, float)) for x in slice_):
                blocks.append(
                    {
                        "marker_i": i,
                        "pk_bytes": [int(x) for x in slice_],
                        "day_key": tokens[i + 3],
                    }
                )
    return blocks


def _extract_entry(
    tokens: list,
    string_table: list[str],
    entry_pk_block: dict,
    food_pk_block: dict,
    refs: dict[str, int],
    default_hours_from_gmt: int,
    user_name: str = "",
) -> FoodLogEntry | None:
    """Build a :class:`FoodLogEntry` from its two PK blocks.

    GWT serializes a ``FoodLogEntry`` in the order
    ``FoodIdentifier → context → meal → nutrients → … → SimplePrimaryKey``
    so the request has the food's stable PK FIRST and the entry's UUID LAST.
    Responses reverse this: in the stream we first see the entry's UUID and
    then the food's PK. The caller passes the blocks in that stream order:
    ``entry_pk_block`` first, ``food_pk_block`` second.
    """
    day_key = entry_pk_block["day_key"]
    mid_start = entry_pk_block["marker_i"] + 4
    mid_end = food_pk_block["marker_i"] - 16

    food_id_code = ""
    if mid_start < len(tokens) and isinstance(tokens[mid_start], str):
        food_id_code = tokens[mid_start]

    # FoodMeasure ordinal — the int immediately before the food_measure ref.
    food_measure_ord = None
    fm_ref = refs.get("food_measure")
    if fm_ref:
        for j in range(mid_start, min(mid_end, mid_start + 20)):
            if tokens[j] == fm_ref and j > 0 and isinstance(tokens[j - 1], (int, float)):
                food_measure_ord = int(tokens[j - 1])
                break

    # Nutrients — server writes them as <value, Double_ref, ordinal, FoodMeasurement_ref>.
    # When multiple entries share identical nutrient values (same food, same
    # servings) GWT deduplicates the HashMap, writing it only once. So scan
    # the entry's mid range first; if empty, fall back to the whole response.
    fmment_ref = refs.get("food_measurement")
    dbl_ref = refs.get("double")

    def _collect_nutrients(start: int, end: int) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        if not (fmment_ref and dbl_ref):
            return out
        for j in range(start, end):
            if (
                tokens[j] == fmment_ref
                and j >= 3
                and tokens[j - 2] == dbl_ref
                and isinstance(tokens[j - 1], int)
                and isinstance(tokens[j - 3], (int, float))
            ):
                ord_ = int(tokens[j - 1])
                if 0 <= ord_ <= 30:
                    out.append((ord_, float(tokens[j - 3])))
        return out

    nutrients_ordered = _collect_nutrients(mid_start, mid_end)
    if not nutrients_ordered:
        nutrients_ordered = _collect_nutrients(0, len(tokens))

    # Meal + extra ordinals.
    # GWT deduplicates enum values across an array of objects: if 3 FoodLogEntries
    # all share meal=snacks, the FoodLogEntryType enum appears once and each entry
    # references it. So when our entry's mid range doesn't contain a meal_ref,
    # we fall back to searching the WHOLE response (typically the dedup'd enum
    # sits just after the last entry's body).
    def _find_ord_before_ref(ref_id: int, search_start: int, search_end: int) -> int | None:
        for j in range(search_start, search_end):
            if tokens[j] == ref_id and j > 0 and isinstance(tokens[j - 1], int):
                return int(tokens[j - 1])
        return None

    meal_ord = 0
    if refs.get("meal"):
        meal_ord = (
            _find_ord_before_ref(refs["meal"], mid_start, mid_end)
            or _find_ord_before_ref(refs["meal"], 0, len(tokens))
            or 0
        )
    extra_ord = 3
    if refs.get("extra"):
        extra_ord = (
            _find_ord_before_ref(refs["extra"], mid_start, mid_end)
            or _find_ord_before_ref(refs["extra"], 0, len(tokens))
            or 3
        )

    # Context (day_num + day_key + hours_from_gmt). In response order, the
    # tokens appear as: ..., hours_from_gmt, day_num, "<context_day_key>", ...
    # Skip "Do…" strings (those are food identifier codes, not day keys).
    context_day_key = ""
    day_num = 0
    hours_from_gmt = default_hours_from_gmt
    for j in range(mid_start, mid_end):
        t = tokens[j]
        if (
            isinstance(t, str)
            and t != day_key
            and not t.startswith("Do")
            and re.match(r"^[A-Za-z0-9_$]+$", t)
            and len(t) >= 5
        ):
            context_day_key = t
            for k in range(j - 1, max(j - 6, mid_start), -1):
                if isinstance(tokens[k], int) and tokens[k] >= 5000:
                    day_num = int(tokens[k])
                    break
            for k in range(j - 1, max(j - 8, mid_start), -1):
                if isinstance(tokens[k], int) and -12 <= tokens[k] <= 14 and tokens[k] != day_num:
                    hours_from_gmt = int(tokens[k])
                    break
            break

    # Food name/brand/category — the FoodIdentifier's string refs are
    # serialized AFTER the food PK marker (the SECOND PK marker in stream).
    # In the response stream the field order is brand_ref, name_ref, then
    # a null/locale, then category_ref. Collect string refs, skipping
    # framework / ProductType / username strings.
    food_category = food_name = food_brand = ""
    after = food_pk_block["marker_i"] + 4
    seen: list[str] = []
    for j in range(after, min(after + 15, len(tokens))):
        t = tokens[j]
        if isinstance(t, int) and 1 <= t <= len(string_table):
            s = string_table[t - 1]
            if (
                s
                and not (s.startswith("com.") or s.startswith("java.") or s.startswith("["))
                and s != user_name
            ):
                seen.append(s)
                if len(seen) >= 3:
                    break
    if len(seen) >= 3:
        food_brand, food_name, food_category = seen[:3]
    elif len(seen) == 2:
        food_name, food_category = seen
    elif len(seen) == 1:
        food_name = seen[0]

    # Servings — the first float in [mid_start+1, mid_start+5).
    servings = 1.0
    for j in range(mid_start + 1, min(mid_start + 5, len(tokens))):
        if isinstance(tokens[j], float):
            servings = float(tokens[j])
            break

    return FoodLogEntry(
        food_pk_response=food_pk_block["pk_bytes"],  # SECOND PK in stream
        entry_pk_response=entry_pk_block["pk_bytes"],  # FIRST PK in stream (UUID)
        entry_day_key=day_key,
        context_day_key=context_day_key,
        day_num=day_num,
        hours_from_gmt=hours_from_gmt,
        meal_ordinal=meal_ord,
        extra_ordinal=extra_ord,
        food_measure_ordinal=food_measure_ord if food_measure_ord is not None else 27,
        servings=servings,
        food_identifier_code=food_id_code,
        food_category=food_category,
        food_name=food_name,
        food_brand=food_brand,
        nutrients_ordered=nutrients_ordered,
    )


def parse_entries(
    text: str,
    default_hours_from_gmt: int = -5,
    user_name: str = "",
) -> list[FoodLogEntry]:
    """Extract every :class:`FoodLogEntry` from a daily-details response."""
    tokens, strings = parse_response(text)
    if not strings:
        return []
    refs = _resolve_refs(strings)
    if not (refs.get("bytes") and refs.get("pk")):
        return []
    bytes_ref = refs["bytes"]
    pk_ref = refs["pk"]
    pk_blocks = _find_pk_blocks(tokens, bytes_ref, pk_ref)

    # Identify each FoodLogEntry by its FOOD PK block — the one whose
    # following token is a "Do…"-prefixed food identifier code. The ENTRY
    # PK block is the IMMEDIATELY following PK block in the stream.
    # The two PKs USUALLY share a day_key, but not always (the entry-PK
    # marker can carry a different short string when other objects in the
    # response reference the entry), so we don't gate on day_key equality.
    fle_ref = refs.get("food_log_entry")
    entries: list[FoodLogEntry] = []
    i = 0
    while i < len(pk_blocks) - 1:
        a = pk_blocks[i]
        after_first = tokens[a["marker_i"] + 4] if a["marker_i"] + 4 < len(tokens) else None
        if not is_food_identifier_code(after_first):
            i += 1
            continue
        b = pk_blocks[i + 1]
        # Reject pairs that look too far apart — a real entry body is < 200 tokens.
        if b["marker_i"] - a["marker_i"] > 200:
            i += 1
            continue
        # Sanity check: the entry PK block should have a FoodLogEntry type
        # ref nearby (within the FoodIdentifier sub-section that follows).
        has_fle_ref = False
        if fle_ref is not None:
            for j in range(b["marker_i"] + 4, min(b["marker_i"] + 20, len(tokens))):
                if tokens[j] == fle_ref:
                    has_fle_ref = True
                    break
        if not has_fle_ref:
            i += 1
            continue
        # a = first PK block in stream = ENTRY PK (entry UUID).
        # b = second PK block in stream = FOOD PK (food's stable PK).
        entry = _extract_entry(
            tokens, strings, a, b, refs, default_hours_from_gmt, user_name=user_name
        )
        if entry is not None:
            entries.append(entry)
        i += 2
    return entries


def get_daily_details(http: HttpClient, target_date: date) -> list[FoodLogEntry]:
    """Fetch + parse today's (or any day's) FoodLogEntry list."""
    day_num = day_number_for(target_date)
    day_key = get_daydate_key(http, day_num) or ""
    text = http.post_rpc(_build_payload(http.config, target_date, day_key))
    return parse_entries(
        text,
        default_hours_from_gmt=http.config.hours_from_gmt,
        user_name=http.config.user_name,
    )


def get_daily_details_raw(http: HttpClient, target_date: date) -> str:
    """Same as :func:`get_daily_details` but returns the raw GWT response.

    Useful for fixture capture in tests.
    """
    day_num = day_number_for(target_date)
    day_key = get_daydate_key(http, day_num) or ""
    return http.post_rpc(_build_payload(http.config, target_date, day_key))
