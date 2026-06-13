"""Pure-function upsert match key for safe-mode restore (Track T7).

This module is the *math* half of the safe-mode restore feature. The
orchestrator wiring (per-day diary fetch, per-day plan execution) lives
in T6 and reaches into the helpers here.

The rationale for the match key is laid out in :mod:`docs/backup-spec`
§4.4 and §7.1. Two practical deviations from the prose:

* The spec writes the match key as ``(food_id, created_at ± 10m)``.
  Empirical analysis of the captured diary fixture (see the docstring
  on :class:`lose_it.models.FoodLogEntry`) showed that ``created_at``
  values cluster around 1970-02-15 — i.e. ``f4`` is **not** a real
  epoch-millis timestamp. ``modified_at`` (``f5``) is real. T7 uses
  ``modified_at ± 10m`` so the rest of the spec's reasoning still
  holds: stable across food-metadata edits, drift-tolerant via a
  small fuzz window.
* Restore is purely additive (spec §7.4). The per-day plan returned by
  :func:`plan_day` therefore reports only the *archive*-side
  partition: which archive entries already exist on the server
  (``matched``) and which need a log call (``missing``). Server-only
  entries are never enumerated — they simply stay on the server.

Everything in this module is a pure function of its inputs. No
network, no clock, no filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Iterable

from lose_it.backup._fs import GrainEntry
from lose_it.core._ids import pk_to_hex
from lose_it.models import FoodLogEntry

# ±10 minute fuzz from spec §7.1. Wide enough to absorb the
# clock drift between an originally-logged entry and its re-log, narrow
# enough that two genuinely-different log events at the same
# food_id don't collide.
DEFAULT_UPSERT_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class UpsertMatch:
    """One archive entry paired with the server entry that "claims" it.

    ``server_entry`` is ``None`` when the archive entry has no server
    counterpart — caller will need to issue a log call for it. Pairs
    are emitted in the order :func:`plan_day` iterates the archive
    entries, so the orchestrator can render a stable progress line.
    """

    grain_entry: GrainEntry
    server_entry: FoodLogEntry | None


@dataclass(frozen=True)
class DayPlan:
    """Per-day plan for safe-mode restore.

    Returned by :func:`plan_day`. ``matched`` is the partition of
    archive entries that already exist on the server (skip during
    restore); ``missing`` is the partition that needs a log call.

    Server entries that don't pair with any archive entry are
    intentionally not reported anywhere on the plan: restore is
    additive (spec §7.4) and never reaches into the server's diary to
    remove things.
    """

    date: _date
    matched: list[UpsertMatch]
    missing: list[GrainEntry]


def _parse_grain_modified_at(s: str) -> datetime | None:
    """Parse a grain file's ``modified_at`` string into a ``datetime``.

    Returns ``None`` on empty string or unparseable input — both
    conservative outcomes feed the "no match" decision in
    :func:`upsert_match`.
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def upsert_match(
    grain_entry: GrainEntry,
    server_entry: FoodLogEntry,
    *,
    window: timedelta = DEFAULT_UPSERT_WINDOW,
) -> bool:
    """Pure boolean: does ``server_entry`` claim ``grain_entry``?

    The match key, per spec §4.4 (with ``modified_at`` substituted for
    ``created_at`` — see this module's docstring):

    * ``server_entry.food_id`` (32-char hex, derived from
      ``food_pk_response``) equals ``grain_entry.food_id``, AND
    * ``abs(server_entry.modified_at - grain_entry.modified_at)`` is
      at most ``window``.

    Returns ``False`` if either side's ``modified_at`` is missing or
    unparseable — the conservative answer that lets the caller log
    rather than silently skip.
    """
    grain_modified = _parse_grain_modified_at(grain_entry.modified_at)
    if grain_modified is None:
        return False
    if server_entry.modified_at is None:
        return False
    if food_id_from_food_log_entry(server_entry) != grain_entry.food_id:
        return False
    return abs(server_entry.modified_at - grain_modified) <= window


def plan_day(
    archive_entries: Iterable[GrainEntry],
    server_entries: Iterable[FoodLogEntry],
    *,
    window: timedelta = DEFAULT_UPSERT_WINDOW,
) -> DayPlan:
    """Compute the per-day matched/missing partition for safe-mode restore.

    For each archive entry, the first un-consumed server entry that
    satisfies :func:`upsert_match` (in input order) wins. A server
    entry can claim at most one archive entry — this prevents a single
    server entry from suppressing multiple legitimate re-logs when an
    archive happens to carry two close-in-time entries for the same
    food_id.

    ``date`` on the returned plan comes from the first archive entry
    when available, or from the first server entry, or defaults to
    ``date.min`` for the (degenerate) empty-empty case. Callers that
    need a specific anchor date should rely on the orchestrator (T6)
    rather than this helper.
    """
    archive_list = list(archive_entries)
    server_list = list(server_entries)

    # Per-index "consumed?" flag keeps the matching greedy-and-stable:
    # first archive entry that matches a given server entry wins.
    consumed = [False] * len(server_list)

    matched: list[UpsertMatch] = []
    missing: list[GrainEntry] = []
    for grain in archive_list:
        hit: FoodLogEntry | None = None
        for idx, server in enumerate(server_list):
            if consumed[idx]:
                continue
            if upsert_match(grain, server, window=window):
                consumed[idx] = True
                hit = server
                break
        if hit is None:
            missing.append(grain)
        else:
            matched.append(UpsertMatch(grain_entry=grain, server_entry=hit))

    # The plan's anchor date is informational. The orchestrator owns
    # the canonical "date being restored" — it's the loop variable in
    # spec §7.1's flowchart.
    if archive_list:
        anchor = archive_list[0].date
    else:
        anchor = _date.min
    return DayPlan(date=anchor, matched=matched, missing=missing)


def food_id_from_food_log_entry(entry: FoodLogEntry) -> str:
    """Derive the 32-char hex food id from a :class:`FoodLogEntry`.

    The grain file carries ``food_id`` as a string (the spec's chosen
    contract — §4.1); ``FoodLogEntry`` carries the underlying PK bytes
    in response form. This helper is the one bridge between the two:
    delegates to the canonical :func:`lose_it.core._ids.pk_to_hex` so
    there's exactly one encoding implementation in the codebase.
    """
    return pk_to_hex(entry.food_pk_response)
