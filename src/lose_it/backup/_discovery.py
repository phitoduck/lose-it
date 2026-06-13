"""Discovery probe: find the earliest day with diary entries.

Implements §5 of ``docs/backup-spec.md``. The cheapest probe shape, given
the bulk range RPC (T0 — :meth:`lose_it.LoseIt.diary_range`), is:

1. One **yearly** range probe per candidate year (``Jan-1 .. Dec-31``).
   The whole year fits in one ``diary_range`` call; the response is a
   ``dict[date, list[FoodLogEntry]]`` keyed by every day in the year, so
   we can spot the first non-empty day directly.
2. Once a year hits, drop to **monthly** range probes inside that year
   (``YYYY-MM-01 .. YYYY-MM-end``) — same RPC, smaller window. First
   month with entries is the hit month.
3. Once a month hits, **walk the month day-by-day** via the per-day
   :meth:`lose_it.LoseIt.diary`. Day-by-day rather than binary-search is
   load-bearing (spec §5.2): a user who only logged Aug-14 and Aug-15 of
   their first month would be skipped by binary search.

Fallback (spec §5): if a yearly range RPC raises
:class:`lose_it.core.daily.TooMuchData` (a "heavy logger" produced a
year-spanning response too large for the server's bulk endpoint), the
algorithm drops to monthly probes for that year — issuing all twelve
monthly probes for the year and picking the earliest hit month for the
day walk. The "drop all the way to twelve" is deliberate: in the
fallback branch we have no yearly response to bound the search, so we
treat the monthly fan-out as the *replacement* for the yearly probe
rather than as a short-circuit. No further recursion is implemented —
monthly probes are already small.

This module is *pure logic over a tiny SDK surface*: it does not write
``index.toon`` itself. The caller (T6's backup orchestrator) consumes
:class:`DiscoveryResult` and decides what to persist. Keeping the
separation makes the discovery probe testable with a ``FakeLoseIt``
double that captures the per-day result map and counts RPCs — see
``tests/conformance/test_backup_discovery.py``.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from datetime import date

from lose_it import LoseIt
from lose_it.core.daily import TooMuchData


@dataclass(frozen=True)
class DiscoveryProbe:
    """Bookkeeping for one probe step the CLI can render.

    The CLI's ``discovering earliest day...`` block (spec §3.1) reads a
    list of these and renders one line per probe. The ``label`` is
    pre-formatted — yearly probes carry the ``"YYYY-MM-DD .. YYYY-MM-DD"``
    bookmark, monthly probes carry ``"YYYY-MM"``, daily probes carry
    ``"YYYY-MM-DD"``.

    ``rpcs`` lets the caller display a running total — every probe step
    here is exactly one RPC, but the field exists so future grain
    primitives (e.g. a "summary" probe that collapses multiple monthly
    calls) can record their cost faithfully without breaking the trace.
    """

    label: str
    hit: bool
    rpcs: int


@dataclass(frozen=True)
class DiscoveryResult:
    """Outcome of :func:`discover_earliest_day`.

    ``earliest_day`` is ``None`` iff the probe found no entries between
    ``probe_from`` and ``today`` (a brand-new account, or one whose data
    predates ``probe_from``).

    ``probes`` is the ordered trace of probe steps the algorithm took;
    the CLI uses it to render the discovery section of ``loseit backup``
    stdout. ``total_rpcs`` is the sum of ``p.rpcs`` for each probe — the
    caller can also derive it but having it pre-summed simplifies the
    BDD-format ``"~N RPCs"`` summary line.
    """

    earliest_day: date | None
    probes: list[DiscoveryProbe] = field(default_factory=list)
    total_rpcs: int = 0


def _earliest_hit_day(
    by_day: dict[date, list],
    *,
    not_before: date | None = None,
) -> date | None:
    """Return the earliest date in ``by_day`` whose list is non-empty.

    ``not_before`` clips out days that precede ``probe_from`` (e.g. when
    a yearly probe spans Jan-1 to Dec-31 but the caller is interested
    only in days ``>= probe_from``).
    """
    hits = [d for d, entries in by_day.items() if entries]
    if not_before is not None:
        hits = [d for d in hits if d >= not_before]
    return min(hits) if hits else None


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Inclusive ``(first_day, last_day)`` of ``year-month``."""
    _, last = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last)


def _clip(start: date, end: date, *, lo: date, hi: date) -> tuple[date, date] | None:
    """Intersect ``[start, end]`` with ``[lo, hi]``; return None if empty."""
    s = max(start, lo)
    e = min(end, hi)
    if s > e:
        return None
    return s, e


def _sleep(sleep_seconds: float) -> None:
    """One sleep guard, factored so tests can avoid timing flake.

    ``sleep_seconds <= 0`` skips the sleep entirely — this is how the
    test double (``FakeLoseIt``) avoids the per-RPC throttle in unit
    tests.
    """
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def _monthly_walk_year(
    li: LoseIt,
    year: int,
    *,
    probe_from: date,
    today: date,
    sleep_seconds: float,
    probes: list[DiscoveryProbe],
    short_circuit: bool,
) -> int | None:
    """Issue monthly probes for ``year`` and return the first hit month.

    Used in two places:

    * The normal **post-yearly-hit narrow** (``short_circuit=True``).
      Called after a yearly probe came back non-empty. Stops at the
      first hit month so the RPC count stays ~7-8 for a typical
      early-month logger (spec §5.4 cost table).
    * The **TooMuchData fallback** for a heavy-logger year
      (``short_circuit=False``). Called when the yearly probe raises.
      Walks every month in the year unconditionally — the fallback
      branch has no yearly response to lean on, so the monthly fan-out
      *replaces* the yearly probe entirely. Issuing all 12 monthly
      probes (vs. 1 yearly) is the worst-case spec §5.4 budget that
      keeps the algorithm bounded for fallback years.

    Returns the first month (1-12) with a hit (clipped to
    ``[probe_from, today]``), or ``None`` if no months in ``year`` had
    entries within that window.
    """
    first_hit: int | None = None
    for month in range(1, 13):
        month_start, month_end = _month_bounds(year, month)
        m_clipped = _clip(month_start, month_end, lo=probe_from, hi=today)
        if m_clipped is None:
            continue
        mstart, mend = m_clipped
        _sleep(sleep_seconds)
        m_by_day = li.diary_range(mstart, mend)
        m_hit = _earliest_hit_day(m_by_day, not_before=probe_from)
        hit_flag = m_hit is not None
        probes.append(
            DiscoveryProbe(
                label=f"{year:04d}-{month:02d}",
                hit=hit_flag,
                rpcs=1,
            )
        )
        if hit_flag and first_hit is None:
            first_hit = month
            if short_circuit:
                return month
    return first_hit


def _day_walk(
    li: LoseIt,
    start: date,
    end: date,
    *,
    sleep_seconds: float,
    probes: list[DiscoveryProbe],
) -> date | None:
    """Walk ``[start, end]`` day-by-day via ``li.diary(d)`` (spec §5.2).

    Day-by-day rather than binary-search: the "lonely 2-day fad diet"
    scenario (logged only Aug-14 and Aug-15 of the first month) would
    be silently skipped by a midpoint probe. The day-walk is bounded
    by ``end - start <= 31`` so the cost stays small.

    Records one :class:`DiscoveryProbe` per day; the caller's ``probes``
    list is appended in-place so the surrounding algorithm's bookkeeping
    stays linear.
    """
    cur = start
    span = (end - start).days
    for _ in range(span + 1):
        _sleep(sleep_seconds)
        entries = li.diary(cur)
        hit_flag = bool(entries)
        probes.append(DiscoveryProbe(label=cur.isoformat(), hit=hit_flag, rpcs=1))
        if hit_flag:
            return cur
        if cur >= end:
            break
        cur = date.fromordinal(cur.toordinal() + 1)
    return None


def discover_earliest_day(
    li: LoseIt,
    *,
    probe_from: date = date(2015, 1, 1),
    today: date,
    sleep_seconds: float = 1.0,
) -> DiscoveryResult:
    """Discover the earliest day with diary entries (spec §5).

    Algorithm (with one fallback branch for heavy-logger years):

    1. **Yearly probes** via ``li.diary_range(year_start, year_end)``
       walking ``probe_from.year`` forward through ``today.year``. The
       first/last year are clipped to ``probe_from`` / ``today``.
       The first year whose response contains any non-empty
       ``DailyDetails`` block is the **hit year**.
    2. **Monthly probes** inside the hit year via
       ``li.diary_range(month_start, month_end)``, walking Jan forward
       (or ``probe_from`` if the hit year is the first year, similar
       clip for the last year). The first month with a hit is the
       **hit month**.
    3. **Day-by-day walk** inside the hit month via ``li.diary(d)``
       starting from ``max(month_start, probe_from)`` (and stopping at
       ``min(month_end, today)``). The first day that returns entries
       is the answer.

    Fallback: if a yearly probe raises
    :class:`lose_it.core.daily.TooMuchData`, we drop straight to
    monthly probes for that year. The monthly fan-out replaces the
    yearly probe — *all* months in the year are probed (Jan..Dec, or
    the appropriate clip when the year contains ``probe_from`` /
    ``today``) so we don't lose the "is this year a hit at all?"
    signal we'd otherwise get from the yearly response.

    Args:
        li: A high-level :class:`lose_it.LoseIt` (or a structural double
            exposing :meth:`diary_range` and :meth:`diary`).
        probe_from: Earliest date the algorithm will consider. Defaults
            to ``2015-01-01`` per spec §5.4.
        today: The upper bound of the search. Required (the caller is
            usually a CLI that has already resolved "today" against
            the user's local timezone).
        sleep_seconds: Seconds between RPCs. ``<= 0`` skips throttling
            — only the test double should ever pass that.

    Returns:
        :class:`DiscoveryResult`. ``earliest_day is None`` iff no entries
        exist anywhere in ``[probe_from, today]``.
    """
    probes: list[DiscoveryProbe] = []

    if probe_from > today:
        # Degenerate input — nothing to probe. Return cleanly rather than
        # raising; the CLI can decide whether this is a user error.
        return DiscoveryResult(earliest_day=None, probes=probes, total_rpcs=0)

    hit_year: int | None = None
    fallback_hit_month: int | None = None  # set only on TooMuchData path

    # ── Step 1: yearly probes (with monthly fallback per year) ──────
    for year in range(probe_from.year, today.year + 1):
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        clipped = _clip(year_start, year_end, lo=probe_from, hi=today)
        if clipped is None:
            continue
        ystart, yend = clipped
        label = f"{ystart.isoformat()} .. {yend.isoformat()}"
        try:
            by_day = li.diary_range(ystart, yend)
        except TooMuchData:
            # Heavy logger — the yearly RPC is too big. Walk every
            # month of this year instead. The walk records its own
            # probes; we do NOT also record a yearly probe (it raised
            # before producing any signal).
            month_hit = _monthly_walk_year(
                li,
                year,
                probe_from=probe_from,
                today=today,
                sleep_seconds=sleep_seconds,
                probes=probes,
                short_circuit=False,
            )
            if month_hit is not None:
                hit_year = year
                fallback_hit_month = month_hit
                break
            # Heavy logger year with no hits — move on.
            _sleep(sleep_seconds)
            continue

        # Normal yearly path: record the probe + check for any hit.
        earliest_in_year = _earliest_hit_day(by_day, not_before=probe_from)
        hit_flag = earliest_in_year is not None
        probes.append(DiscoveryProbe(label=label, hit=hit_flag, rpcs=1))
        if hit_flag:
            hit_year = year
            break
        _sleep(sleep_seconds)

    if hit_year is None:
        # Walked every year, no entries anywhere. Brand-new account.
        return DiscoveryResult(
            earliest_day=None,
            probes=probes,
            total_rpcs=sum(p.rpcs for p in probes),
        )

    # ── Step 2: monthly probes inside the hit year ──────────────────
    # If we got here via the fallback branch, the monthly walk has
    # already happened and ``fallback_hit_month`` holds the answer —
    # skip the redundant monthly fan-out.
    if fallback_hit_month is not None:
        hit_month = fallback_hit_month
    else:
        hit_month_opt = _monthly_walk_year(
            li,
            hit_year,
            probe_from=probe_from,
            today=today,
            sleep_seconds=sleep_seconds,
            probes=probes,
            short_circuit=True,
        )
        if hit_month_opt is None:
            # Shouldn't happen — the yearly probe said there WAS a hit —
            # but be defensive: return nothing rather than crash.
            return DiscoveryResult(
                earliest_day=None,
                probes=probes,
                total_rpcs=sum(p.rpcs for p in probes),
            )
        hit_month = hit_month_opt

    # ── Step 3: day-by-day walk inside the hit month ────────────────
    month_start, month_end = _month_bounds(hit_year, hit_month)
    d_clipped = _clip(month_start, month_end, lo=probe_from, hi=today)
    # The monthly probe said this window had a hit, so the clip is
    # always non-empty here.
    assert d_clipped is not None
    dstart, dend = d_clipped
    earliest = _day_walk(
        li,
        dstart,
        dend,
        sleep_seconds=sleep_seconds,
        probes=probes,
    )
    return DiscoveryResult(
        earliest_day=earliest,
        probes=probes,
        total_rpcs=sum(p.rpcs for p in probes),
    )
