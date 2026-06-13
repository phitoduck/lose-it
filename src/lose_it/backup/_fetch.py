"""Fetch primitive for the backup feature (T2).

Implements §6 of ``docs/backup-spec.md``: the conceptual unit of work
is a **grain** (day / week / month), not a day. The primitive tries
``LoseIt.diary_range`` for the whole grain in one shot; if the server
responds with oversize/throttling, it splits the grain into the
next-smaller grain and retries each piece. The recursion floor is one
day — if a single-day fetch fails the caller aborts.

Three public surfaces live in this module:

* :class:`Grain` — value type wrapping a ``(kind, start, end)`` triple
  plus the canonical splitter (``split_one_step``).
* :func:`fetch_grain` — the orchestrator. Wraps ``LoseIt.diary_range``
  + ``LoseIt.diary`` with the recursive split-and-retry described in
  the spec, returning the accumulated entries plus a
  :class:`FetchStatus` (``fetch`` for clean first attempts,
  ``fallback`` whenever recursion happened).
* :func:`update_food_cache` — applies the once-per-UTC-calendar-day
  describe rule from spec §6.3 to a ``foods.toon`` file on disk.

The helper :func:`to_grain_entry` projects a ``FoodLogEntry`` (the
SDK's wire-shaped row) into the ``GrainEntry`` shape T1 owns. It is
used by the backup orchestrator (T6) when it serializes the entries
returned by :func:`fetch_grain` into a grain file.

Notes for readers tracing the spec back to this file:

* Spec §4.1 calls out the sort key ``(day_num, meal_ordinal,
  created_at)``. Empirical analysis of the wire shape (the work that
  shipped under T4) showed ``FoodLogEntry.created_at`` (extracted from
  FLE.f4) is **not a real timestamp** — its values cluster around
  1970-02-15, which dates the field is some kind of opaque counter
  rather than an epoch-ms long. Only ``modified_at`` (from FLE.f5) is
  a real UTC timestamp. We therefore substitute ``modified_at`` into
  the sort tuple. The on-disk row still carries both fields (T1
  wrote the schema to allow either), so downstream tooling that
  later proves out ``created_at`` semantics doesn't lose information.
* Spec §6.3 says the describe-cadence gate is "the UTC calendar day
  of ``last_described_at``." :func:`update_food_cache` takes
  ``today_utc`` as an injectable parameter so the unit tests can
  freeze the gate without monkey-patching the system clock.
* Spec §6.4 talks about "atomic checkpoint per grain." The grain-file
  write is owned by T6 (the orchestrator). T2 only owns the
  ``foods.toon`` write inside :func:`update_food_cache`, which goes
  through :func:`lose_it.backup._fs.write_foods_file` and inherits
  the atomic-write guarantee from there.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from lose_it.backup._fs import (
    FoodCacheEntry,
    FoodsDoc,
    GrainEntry,
    read_foods_file,
    write_foods_file,
)
from lose_it.core._ids import pk_to_hex
from lose_it.core.daily import TooMuchData
from lose_it.models import FoodLogEntry


class FetchStatus(Enum):
    """Per-grain status reported by :func:`fetch_grain`.

    Mirrors the four statuses spec §3.1 documents in the CLI summary
    table. T2 only emits two of them (``fetch`` and ``fallback`` — the
    other two are filesystem-level statuses owned by T6's orchestrator),
    but the enum is the single source of truth across the codebase so
    they live together.
    """

    skip = "skip"
    partial = "partial"
    fetch = "fetch"
    fallback = "fallback"


# ── Grain value type ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Grain:
    """A ``(kind, start, end)`` triple covering one backup grain.

    ``kind`` is one of ``"day"``, ``"week"``, ``"month"``. Construction is
    usually via the named constructors so the bounds are computed from
    a single "any day inside this grain" reference instead of the caller
    doing calendar arithmetic.

    The split policy is also encoded here so that the splitter and the
    grain bounds can never disagree about what "the next smaller grain"
    means. ``split_one_step`` is the only place month→week→day is
    spelled out.
    """

    kind: str  # "day" | "week" | "month"
    start: date
    end: date

    # ── Constructors ─────────────────────────────────────────────────────────

    @staticmethod
    def month(any_day_in_month: date) -> Grain:
        """Build the month-grain covering ``any_day_in_month``.

        Bounds are first-of-month through last-of-month inclusive.
        """
        start = any_day_in_month.replace(day=1)
        # Next-month-first minus one day = last day of this month. We
        # avoid calendar.monthrange so the function stays import-light.
        if start.month == 12:
            next_month_first = start.replace(year=start.year + 1, month=1)
        else:
            next_month_first = start.replace(month=start.month + 1)
        end = next_month_first - timedelta(days=1)
        return Grain(kind="month", start=start, end=end)

    @staticmethod
    def week(any_day_in_iso_week: date) -> Grain:
        """Build the ISO-week grain (Mon..Sun) containing ``any_day_in_iso_week``.

        ISO week numbering is what the CLI's ``YYYY/Www.toon`` layout
        encodes (spec §2), so the splitter naturally hands a
        :class:`Grain` whose ``start`` is a Monday and ``end`` is the
        Sunday seven days later.
        """
        # ``isocalendar()[2]`` is 1..7 with Monday=1.
        weekday_iso = any_day_in_iso_week.isocalendar()[2]
        monday = any_day_in_iso_week - timedelta(days=weekday_iso - 1)
        sunday = monday + timedelta(days=6)
        return Grain(kind="week", start=monday, end=sunday)

    @staticmethod
    def day(d: date) -> Grain:
        """Build a single-day grain at ``d``."""
        return Grain(kind="day", start=d, end=d)

    # ── Splitter ─────────────────────────────────────────────────────────────

    def split_one_step(self) -> list[Grain]:
        """Split into the next-smaller grain.

        * ``month`` → list of ISO-week grains whose union covers the
          month (typically 5 weeks, sometimes 4 or 6). Weeks at the
          boundaries are NOT trimmed to the month — the splitter's job
          is to pick a smaller fetch unit, and trimming would violate
          ISO-week alignment that callers later use to name files.
          Callers that need month-aligned bounds should filter the
          returned entries by date themselves.
        * ``week`` → list of 7 day-grains.
        * ``day`` → :class:`ValueError`. The recursion floor of the
          fetch primitive is one day; below that there is nothing
          smaller to try.

        The returned grains are ordered chronologically.
        """
        if self.kind == "day":
            raise ValueError("cannot split a day-grain — recursion floor reached")
        if self.kind == "week":
            return [Grain.day(self.start + timedelta(days=i)) for i in range(7)]
        # month → weeks. Walk forward week-by-week starting from the
        # ISO week of the first-of-month; stop once we've passed the
        # month-end. The first/last week of a month frequently spills
        # into the prior/next month — that's a feature, not a bug
        # (we'd otherwise need an irregular "partial week" grain).
        weeks: list[Grain] = []
        cursor = self.start
        while cursor <= self.end:
            w = Grain.week(cursor)
            weeks.append(w)
            cursor = w.end + timedelta(days=1)
        return weeks


# ── Fetch primitive ──────────────────────────────────────────────────────────


class _DiaryRangeProto(Protocol):
    """Structural typing for the bits of :class:`LoseIt` we actually call.

    Only used internally for type-checking the parameter to
    :func:`fetch_grain` — tests can pass any class with these two
    methods without inheriting from anything.
    """

    def diary_range(self, start: date, end: date) -> dict[date, list[FoodLogEntry]]: ...

    def diary(self, when: date) -> list[FoodLogEntry]: ...


def fetch_grain(
    li: _DiaryRangeProto,
    grain: Grain,
    *,
    sleep_seconds: float = 1.0,
) -> tuple[list[FoodLogEntry], FetchStatus]:
    """Fetch one grain, recursing on :class:`TooMuchData`.

    Tries ``li.diary_range(grain.start, grain.end)`` first. On a clean
    success, returns the flattened list of entries with status
    ``FetchStatus.fetch``.

    On :class:`TooMuchData`:

    * If the grain is a single day (``grain.kind == "day"``) the
      exception propagates — there is no smaller grain to retry.
    * Otherwise the grain is split via :meth:`Grain.split_one_step`
      and each sub-grain is fetched recursively. ``sleep_seconds`` is
      slept between sub-grain calls to keep the load on the server
      polite (spec §6.3). The accumulated entries from every sub-grain
      are returned with status ``FetchStatus.fallback``.

    A nested fallback (month splits to weeks, one of those weeks then
    splits to days) still reports ``FetchStatus.fallback`` at the top
    level — the status describes "did any recursion happen for this
    grain?" not the depth of it.
    """
    try:
        per_day = li.diary_range(grain.start, grain.end)
    except TooMuchData:
        if grain.kind == "day":
            # Recursion floor — caller (T6 orchestrator) is responsible
            # for aborting the whole backup at this point.
            logger.warning(
                "fetch_grain: day-grain {d} still TooMuchData — re-raising",
                d=grain.start.isoformat(),
            )
            raise
        return _fetch_with_split(li, grain, sleep_seconds=sleep_seconds)

    # Clean fetch — flatten the day-keyed map into a single list. The
    # caller decides how to bucket it back; the splitter contract says
    # "give me everything in the grain."
    entries: list[FoodLogEntry] = []
    for _, day_entries in sorted(per_day.items()):
        entries.extend(day_entries)
    return entries, FetchStatus.fetch


def _fetch_with_split(
    li: _DiaryRangeProto,
    grain: Grain,
    *,
    sleep_seconds: float,
) -> tuple[list[FoodLogEntry], FetchStatus]:
    """Split ``grain`` and recursively fetch each sub-grain.

    Always returns ``FetchStatus.fallback`` — by definition the caller
    only invokes this after the parent grain's first attempt raised.
    """
    sub_grains = grain.split_one_step()
    logger.info(
        "fetch_grain: splitting {kind} {s}..{e} into {n} sub-grains",
        kind=grain.kind,
        s=grain.start.isoformat(),
        e=grain.end.isoformat(),
        n=len(sub_grains),
    )
    accumulated: list[FoodLogEntry] = []
    for i, sub in enumerate(sub_grains):
        if i > 0 and sleep_seconds > 0:
            # Between sub-grain calls only — spec §6.3 wants a pause
            # between RPCs but we don't need a leading pause before the
            # first one (the parent's failed RPC already cost us time).
            time.sleep(sleep_seconds)
        sub_entries, _ = fetch_grain(li, sub, sleep_seconds=sleep_seconds)
        accumulated.extend(sub_entries)
    return accumulated, FetchStatus.fallback


# ── Describe-cadence ─────────────────────────────────────────────────────────


class _DescribeFoodProto(Protocol):
    """Structural typing for the describe-food side of :class:`LoseIt`."""

    def describe_food(self, food_id: str) -> Any: ...


def _now_utc() -> datetime:
    """Indirected for tests that want to freeze "now" without monkey-patching."""
    return datetime.now(UTC)


def update_food_cache(
    li: _DescribeFoodProto,
    foods_path: Path,
    seen_food_ids: Iterable[str],
    *,
    sleep_seconds: float = 1.0,
    today_utc: date | None = None,
) -> int:
    """Describe each ``food_id`` at most once per UTC calendar day.

    Implements spec §6.3: for every ``food_id`` in ``seen_food_ids``,

    * if not already in ``foods.toon`` → describe it now,
    * if ``last_described_at``'s UTC date matches ``today_utc`` → skip,
    * otherwise → describe it again (the SCD recapture path).

    The describe RPCs are issued **serially** with ``sleep_seconds``
    between calls. Concurrent calls against the same endpoint family
    have been observed to trip rate limits.

    ``today_utc`` defaults to ``datetime.now(UTC).date()``; tests can
    pin it to a specific calendar day to exercise the gate without
    monkey-patching the system clock.

    Returns the count of describe RPCs sent. The updated
    :class:`FoodsDoc` is written back atomically via
    :func:`lose_it.backup._fs.write_foods_file`.

    The function loads ``foods_path`` if it exists; the orchestrator
    is responsible for initializing the file (with the right
    :class:`~lose_it.backup._fs.AccountRef`) before the first call. If
    the file is missing, the function raises ``FileNotFoundError`` —
    surfacing the missing-account-binding bug rather than silently
    creating an unbound file.
    """
    if today_utc is None:
        today_utc = _now_utc().date()

    doc = read_foods_file(foods_path)

    # Deduplicate seen ids while preserving first-seen order so the
    # describe loop's wire ordering is deterministic across runs.
    seen_unique: list[str] = []
    seen_set: set[str] = set()
    for fid in seen_food_ids:
        if fid not in seen_set:
            seen_set.add(fid)
            seen_unique.append(fid)

    describe_count = 0
    updated_foods: dict[str, FoodCacheEntry] = dict(doc.foods)

    for food_id in seen_unique:
        existing = updated_foods.get(food_id)
        if existing is not None:
            last_date = _parse_iso_date_portion(existing.last_described_at)
            if last_date == today_utc:
                # Spec §6.3: same UTC day → no second describe.
                continue

        if describe_count > 0 and sleep_seconds > 0:
            # Between describes; no leading sleep on the very first one
            # so a single-food cache update doesn't add a pointless 1s
            # delay.
            time.sleep(sleep_seconds)

        description = li.describe_food(food_id)
        describe_count += 1
        now_iso = _now_utc().isoformat()
        updated_foods[food_id] = _to_food_cache_entry(
            description=description,
            food_id=food_id,
            last_described_at=now_iso,
            first_seen_date=existing.first_seen_date if existing else today_utc,
            last_seen_date=today_utc,
        )

    if describe_count == 0:
        # Nothing changed — skip the write so we don't churn mtimes /
        # gratuitously rewrite the file.
        logger.debug(
            "update_food_cache: 0 RPCs (all {n} ids already described today UTC)",
            n=len(seen_unique),
        )
        return 0

    new_doc = FoodsDoc(
        account=doc.account,
        foods=updated_foods,
        schema_version=doc.schema_version,
    )
    write_foods_file(foods_path, new_doc)
    logger.info(
        "update_food_cache: sent {n} describe RPCs (out of {total} seen)",
        n=describe_count,
        total=len(seen_unique),
    )
    return describe_count


def _parse_iso_date_portion(iso_ts: str) -> date | None:
    """Pull the calendar-date portion out of an ISO 8601 timestamp.

    Returns ``None`` if the string is empty or doesn't parse as ISO.
    Used by the describe-cadence gate to compare against ``today_utc``.
    """
    if not iso_ts:
        return None
    try:
        # ``fromisoformat`` accepts ``+00:00`` offsets natively since
        # Python 3.11; the dataclass stores wall-clock UTC so the
        # offset is always present in our writes.
        return datetime.fromisoformat(iso_ts).astimezone(UTC).date()
    except ValueError:
        return None


def _to_food_cache_entry(
    *,
    description: Any,
    food_id: str,
    last_described_at: str,
    first_seen_date: date,
    last_seen_date: date,
) -> FoodCacheEntry:
    """Project a :class:`~lose_it.models.FoodDescription` into the cache shape.

    Kept tolerant of the description object's exact type — anything
    with the documented attributes works, which keeps the test fakes
    simple. The fields lifted here mirror spec §4.2's ``foods.toon``
    schema.
    """
    primary = getattr(description, "primary_serving", None)
    cross = getattr(description, "cross_class_conversion", None)
    primary_dict = primary.to_dict() if primary is not None and hasattr(primary, "to_dict") else {}
    cross_dict = cross.to_dict() if cross is not None and hasattr(cross, "to_dict") else {}
    return FoodCacheEntry(
        food_id=food_id,
        last_described_at=last_described_at,
        first_seen_date=first_seen_date,
        last_seen_date=last_seen_date,
        name=str(getattr(description, "name", "") or ""),
        brand=str(getattr(description, "brand", "") or ""),
        category=str(getattr(description, "category", "") or ""),
        primary_serving=dict(primary_dict),
        cross_class_conversion=dict(cross_dict),
        nutrients_per_serving={
            str(k): float(v)
            for k, v in (getattr(description, "nutrients_per_serving", {}) or {}).items()
        },
        raw_nutrients_by_ord={
            str(k): float(v)
            for k, v in (getattr(description, "raw_nutrients_by_ord", {}) or {}).items()
        },
    )


# ── GrainEntry projection ────────────────────────────────────────────────────


def to_grain_entry(
    fle: FoodLogEntry,
    *,
    entry_date: date,
    food_description: dict[str, Any] | None = None,
    ingest_ts: str,
) -> GrainEntry:
    """Project a :class:`FoodLogEntry` into the on-disk
    :class:`~lose_it.backup._fs.GrainEntry` shape (spec §4.1).

    ``food_description`` is the row from ``foods.toon`` for this
    entry's ``food_id``. It is consulted for ``food_name`` / ``brand``
    / ``category`` whenever the wire-shaped FLE didn't carry those
    fields. The FLE *does* usually carry them — but the diary range
    response has been observed to leave the ``food_name`` blank for
    historical entries whose food was renamed server-side, and the
    cache is the canonical source there.

    ``ingest_ts`` is the "when this backup recorded the row" timestamp;
    the caller (T6 orchestrator) passes a single value reused for every
    entry written in the same flush so they share a coherent
    generation moment.

    The ``created_at`` / ``modified_at`` fields are serialized as ISO
    8601 strings with ``+00:00`` offsets. Empirical analysis (see
    module docstring) showed ``created_at`` (FLE.f4) is not a real
    epoch-ms timestamp, but T1's :class:`GrainEntry` carries it
    verbatim — downstream sorters use ``modified_at`` instead.
    """
    fname = fle.food_name or ((food_description or {}).get("name") or "")
    fbrand = fle.food_brand or ((food_description or {}).get("brand") or "")
    fcat = fle.food_category or ((food_description or {}).get("category") or "")

    created_iso = fle.created_at.isoformat() if fle.created_at else ""
    modified_iso = fle.modified_at.isoformat() if fle.modified_at else ""

    # ``FoodLogEntry`` doesn't (yet) expose a ``food_id`` property at the
    # delete-safeguards baseline this track is built on. Derive the
    # 32-char hex identity here from the same ``food_pk_response`` bytes
    # the spec's §4.4 upsert key consumes.
    food_id = pk_to_hex(fle.food_pk_response) if len(fle.food_pk_response) == 16 else ""

    return GrainEntry(
        date=entry_date,
        day_num=int(fle.day_num),
        meal=fle.meal_name,
        meal_ordinal=int(fle.meal_ordinal),
        food_id=food_id,
        food_name=str(fname),
        food_brand=str(fbrand),
        food_category=str(fcat),
        food_identifier_code=str(fle.food_identifier_code or ""),
        food_measure_ordinal=int(fle.food_measure_ordinal),
        food_measure_unit=fle.food_measure_unit,
        servings=float(fle.servings),
        calories=fle.calories,
        nutrients={str(ord_): float(val) for ord_, val in fle.nutrients_ordered},
        nutrients_by_label=dict(fle.nutrients_by_label),
        entry_pk_response=list(fle.entry_pk_response),
        food_pk_response=list(fle.food_pk_response),
        entry_day_key=str(fle.entry_day_key or ""),
        context_day_key=str(fle.context_day_key or ""),
        hours_from_gmt=int(fle.hours_from_gmt),
        created_at=created_iso,
        modified_at=modified_iso,
        ingest_ts=ingest_ts,
    )


# ── Sort key ─────────────────────────────────────────────────────────────────


def grain_entry_sort_key(e: GrainEntry) -> tuple[int, int, str]:
    """Spec §4.1 sort key for a :class:`GrainEntry`, with one substitution.

    The spec defines the canonical ordering as
    ``(day_num asc, meal_ordinal asc, created_at asc)``. Empirical
    analysis under T4 showed ``created_at`` (extracted from FLE.f4) is
    NOT a real epoch-ms timestamp — values cluster around
    ``1970-02-15``. Only ``modified_at`` (FLE.f5) is a real UTC epoch.
    So this key uses :attr:`GrainEntry.modified_at` instead of
    :attr:`GrainEntry.created_at`. The on-disk schema (T1) carries
    both fields, so a future change that proves out a real
    ``created_at`` semantic doesn't lose information — only the sort
    tiebreaker changes.

    Used by T6 when writing a grain file. Exposed at module level so
    the unit test that pins this invariant can import the same
    function the producer code uses.
    """
    return (int(e.day_num), int(e.meal_ordinal), e.modified_at)
