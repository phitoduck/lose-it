"""Conformance tests for the earliest-day discovery probe (T3).

Hermetic: a ``FakeLoseIt`` double exposes the bare-minimum SDK surface
the algorithm under test consumes (``diary_range`` + ``diary``) and
records every call so the tests can assert on RPC count + walk shape.

Spec coverage:

* §5.1 — yearly-range probe is correct for accounts that started
  logging late in a year (test :func:`test_start_in_late_year_works`).
* §5.2 — the day-narrow walks day-by-day, not via binary search
  (test :func:`test_discovery_walks_first_month_day_by_day_not_binary_search`).
* §5.4 — the cost bound: ~5 yearly + ~7 monthly + ~14 daily ≈ 26 RPCs
  for Eric's profile (test :func:`test_finds_august_2019_earliest_for_typical_profile`).
* §5 fallback — monthly probes for a heavy-logger year
  (test :func:`test_falls_back_to_monthly_probes_on_oversize_year`).
* "no entries ever" terminal case
  (test :func:`test_no_entries_ever_returns_none`).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from lose_it.backup import discover_earliest_day
from lose_it.core.daily import TooMuchData


class FakeLoseIt:
    """Structural double for the SDK surface ``discover_earliest_day`` uses.

    The algorithm calls :meth:`diary_range` (bulk) and :meth:`diary`
    (single day). The fake returns the pre-seeded ``entries_by_day``
    map for every queried date, records the call list, and can be
    configured to raise :class:`TooMuchData` for arbitrarily large
    ranges (the heavy-logger fallback test).
    """

    def __init__(
        self,
        entries_by_day: dict[date, list[Any]],
        *,
        raise_too_much_for_years: set[int] | None = None,
        oversize_threshold_days: int | None = None,
    ) -> None:
        self.entries_by_day = dict(entries_by_day)
        self.diary_range_calls: list[tuple[date, date]] = []
        self.diary_calls: list[date] = []
        # The heavy-logger fallback test wants a specific year's yearly
        # probe to raise — ``raise_too_much_for_years`` opts that in.
        self._raise_too_much_for_years = set(raise_too_much_for_years or ())
        # Belt-and-suspenders for the spec §5 wording: any range that
        # spans more than ``oversize_threshold_days`` days raises
        # TooMuchData. None = never raise by size.
        self._oversize_threshold_days = oversize_threshold_days

    # ── SDK surface ────────────────────────────────────────────────

    def diary_range(self, start: date, end: date) -> dict[date, list[Any]]:
        self.diary_range_calls.append((start, end))
        # The "heavy logger" branch: a yearly RPC for a flagged year
        # raises before producing any signal. The monthly RPCs that
        # follow are (start.year == flagged AND start..end <= 31 days)
        # so they DON'T trip the guard.
        span_days = (end - start).days + 1
        if start.year in self._raise_too_much_for_years and span_days > 60:
            raise TooMuchData(
                f"fake oversize for {start.year} (range {start}..{end} = {span_days}d)"
            )
        if self._oversize_threshold_days is not None and span_days > self._oversize_threshold_days:
            raise TooMuchData(f"fake oversize for range {start}..{end} = {span_days}d")
        out: dict[date, list[Any]] = {}
        cur = start
        while cur <= end:
            out[cur] = list(self.entries_by_day.get(cur, []))
            cur = cur + timedelta(days=1)
        return out

    def diary(self, when: date) -> list[Any]:
        self.diary_calls.append(when)
        return list(self.entries_by_day.get(when, []))


# ── Tests ──────────────────────────────────────────────────────────────


def test_no_entries_ever_returns_none() -> None:
    """A brand-new account with no entries anywhere produces ``None``.

    Walks 2015..2026 as yearly probes. None hit -> ``earliest_day``
    is ``None``. The cost is ~11-12 yearly RPCs (one per year in the
    inclusive range ``[probe_from.year, today.year]``).
    """
    li = FakeLoseIt(entries_by_day={})
    result = discover_earliest_day(
        li,
        probe_from=date(2015, 1, 1),
        today=date(2026, 6, 12),
        sleep_seconds=0.0,
    )
    assert result.earliest_day is None
    # 12 yearly probes: 2015..2026 inclusive (the last year is
    # clipped to today=2026-06-12 but still issued as a yearly
    # probe at month-1 .. today granularity).
    assert 10 <= len(li.diary_range_calls) <= 13, (
        f"expected ~11 yearly RPCs, got {len(li.diary_range_calls)}: {li.diary_range_calls!r}"
    )
    # All probes were marked as misses.
    assert all(not p.hit for p in result.probes)
    # No day-level probes — never narrowed in.
    assert li.diary_calls == []
    # Reported RPC total matches the probe trace.
    assert result.total_rpcs == sum(p.rpcs for p in result.probes)


def test_finds_august_2019_earliest_for_typical_profile() -> None:
    """Eric's profile: yearly hit on 2019, monthly hit on Aug, day on 14.

    The fake's only entry is 2019-08-14. The algorithm probes
    2015-2018 (4 yearly misses), 2019 (yearly hit), Jan-Aug 2019
    (monthly: 7 empty + 1 hit), then walks Aug 1..14 (14 days, last
    is the hit).

    Total ≈ 5 + 8 + 14 = 27 RPCs (the spec's "~26"; the tilde absorbs
    the off-by-one between "Jan-Jul empty + Aug hit" and "the spec
    table's 7 monthly probes").
    """
    li = FakeLoseIt(entries_by_day={date(2019, 8, 14): ["entry"]})
    result = discover_earliest_day(
        li,
        probe_from=date(2015, 1, 1),
        today=date(2026, 6, 12),
        sleep_seconds=0.0,
    )
    assert result.earliest_day == date(2019, 8, 14)

    # Yearly probes span the full year Jan-1 .. Dec-31. There are 5:
    # 2015, 2016, 2017, 2018, 2019 (the last is the hit).
    yearly_calls = [
        (s, e)
        for s, e in li.diary_range_calls
        if s.month == 1 and s.day == 1 and e.month == 12 and e.day == 31
    ]
    assert len(yearly_calls) == 5

    # 8 monthly probes within 2019 (Jan..Aug). The Aug probe is the hit.
    # Monthly probes are everything in 2019 that isn't a yearly probe.
    monthly_calls = [
        (s, e) for s, e in li.diary_range_calls if s.year == 2019 and (s, e) not in yearly_calls
    ]
    assert len(monthly_calls) == 8
    assert monthly_calls[-1][0] == date(2019, 8, 1)

    # 14 day-level probes in Aug 2019 (1..14).
    assert li.diary_calls == [date(2019, 8, d) for d in range(1, 15)]

    # Total RPC budget for Eric's profile: 5 yearly + 8 monthly + 14 daily = 27.
    # Spec §5.4 gives "~10-30 RPCs" as the typical band; 27 sits comfortably
    # inside the "~26" estimate the prompt sets.
    assert 24 <= result.total_rpcs <= 30, (
        f"expected ~26 RPCs for Eric's profile, got {result.total_rpcs}"
    )
    assert result.total_rpcs == sum(p.rpcs for p in result.probes)


def test_discovery_walks_first_month_day_by_day_not_binary_search() -> None:
    """Spec §5.2 explicitly rules out binary search.

    The "lonely 2-day fad diet" — only Aug 14 and Aug 15 of the first
    month have entries. Binary search at the midpoint of Aug (Aug-16)
    would see an empty diary and drift forward past the actual
    earliest day. The day-by-day walk must touch every day from
    Aug-01 to Aug-14 in order.
    """
    li = FakeLoseIt(
        entries_by_day={
            date(2019, 8, 14): ["entry-a"],
            date(2019, 8, 15): ["entry-b"],
        }
    )
    result = discover_earliest_day(
        li,
        probe_from=date(2015, 1, 1),
        today=date(2026, 6, 12),
        sleep_seconds=0.0,
    )
    assert result.earliest_day == date(2019, 8, 14)
    # Day-by-day from Aug-01 through Aug-14 (the hit).
    expected_day_walk = [date(2019, 8, d) for d in range(1, 15)]
    assert li.diary_calls == expected_day_walk
    # The probe trace's daily entries are in order and the last one
    # is the hit.
    daily_probes = [
        p for p in result.probes if p.label.startswith("2019-08-") and len(p.label) == 10
    ]
    assert [p.label for p in daily_probes] == [d.isoformat() for d in expected_day_walk]
    assert daily_probes[-1].hit is True
    assert all(p.hit is False for p in daily_probes[:-1])


def test_falls_back_to_monthly_probes_on_oversize_year() -> None:
    """Heavy logger: yearly probe for 2019 raises -> monthly fan-out.

    The fake raises :class:`TooMuchData` for the 2019 yearly RPC but
    accepts month-sized ranges. The algorithm must:

    * walk 2015-2018 as normal yearly probes (4 RPCs, all miss),
    * attempt the 2019 yearly probe (raises -> 0 net RPCs recorded
      in the probe trace, but the raised call still hits
      ``diary_range_calls``),
    * issue all 12 monthly probes for 2019 (fallback mode is
      unconditional — see ``_monthly_walk_year(short_circuit=False)``
      in ``_discovery.py``),
    * walk Aug 1..14 day-by-day (14 RPCs).

    The end state ``earliest_day`` is still 2019-08-14. The test
    pins the fact that *only* 2019 fell back — every other year
    stayed at the yearly grain.
    """
    li = FakeLoseIt(
        entries_by_day={date(2019, 8, 14): ["entry"]},
        raise_too_much_for_years={2019},
    )
    result = discover_earliest_day(
        li,
        probe_from=date(2015, 1, 1),
        today=date(2026, 6, 12),
        sleep_seconds=0.0,
    )
    assert result.earliest_day == date(2019, 8, 14)

    # Slice the diary_range_calls by year for inspection.
    by_year: dict[int, list[tuple[date, date]]] = {}
    for s, e in li.diary_range_calls:
        by_year.setdefault(s.year, []).append((s, e))

    # 2015-2018: exactly one yearly probe each (Jan-1 .. Dec-31).
    for year in (2015, 2016, 2017, 2018):
        assert len(by_year.get(year, [])) == 1
        s, e = by_year[year][0]
        assert (s, e) == (date(year, 1, 1), date(year, 12, 31))

    # 2019: 1 yearly attempt (raises) + 12 monthly probes = 13 calls.
    assert len(by_year[2019]) == 13
    # First call is the yearly attempt.
    assert by_year[2019][0] == (date(2019, 1, 1), date(2019, 12, 31))
    # Next 12 are the monthly fan-out (Jan..Dec).
    monthly_2019 = by_year[2019][1:]
    assert len(monthly_2019) == 12
    for idx, (s, e) in enumerate(monthly_2019, start=1):
        assert s.month == idx and s.day == 1
        assert e.month == idx
        assert e.year == 2019

    # No year after 2019 was probed (we found the hit and stopped).
    for year in range(2020, 2027):
        assert year not in by_year, (
            f"expected no probes for {year} (algorithm should have stopped "
            f"after finding 2019 hit); got {by_year[year]!r}"
        )

    # Day-walk in Aug 2019 fired 14 times — same as the normal path.
    assert li.diary_calls == [date(2019, 8, d) for d in range(1, 15)]


def test_start_in_late_year_works() -> None:
    """Spec §5.1: a user who started logging late in a year is found correctly.

    The "Nov 17" case spec §5.1 calls out specifically: a single-day
    yearly probe at Jan-1 would miss this entirely. The
    yearly-range RPC sees the whole year so it doesn't fall into
    that trap.
    """
    li = FakeLoseIt(entries_by_day={date(2020, 11, 17): ["entry"]})
    result = discover_earliest_day(
        li,
        probe_from=date(2015, 1, 1),
        today=date(2026, 6, 12),
        sleep_seconds=0.0,
    )
    assert result.earliest_day == date(2020, 11, 17)

    # Yearly probes 2015..2020 = 6 (last is the hit).
    yearly_calls = [
        (s, e)
        for s, e in li.diary_range_calls
        if s.month == 1 and s.day == 1 and e.month == 12 and e.day == 31
    ]
    assert len(yearly_calls) == 6
    assert yearly_calls[-1] == (date(2020, 1, 1), date(2020, 12, 31))

    # Monthly probes inside 2020: Jan..Nov (10 misses + 1 hit = 11 probes,
    # because we short-circuit on Nov, the hit month). Exclude the yearly
    # 2020 probe (Jan-1 .. Dec-31) from the count.
    monthly_2020 = [
        (s, e)
        for s, e in li.diary_range_calls
        if s.year == 2020 and s.day == 1 and (s, e) not in yearly_calls
    ]
    assert [s.month for s, _ in monthly_2020] == list(range(1, 12))

    # Day-walk inside Nov 2020: Nov-01 .. Nov-17 = 17 days.
    assert li.diary_calls == [date(2020, 11, d) for d in range(1, 18)]
