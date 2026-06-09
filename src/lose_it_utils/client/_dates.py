"""Date / day-number conversion.

LoseIt uses a "day number" — an integer that increments by 1 per calendar day
since some epoch. We anchor on a single observed mapping and arithmetic-shift
to convert dates ↔ day numbers without an extra round-trip.
"""
from __future__ import annotations

from datetime import date, datetime

from ._config import DAY_NUM_ANCHOR_DATE, DAY_NUM_ANCHOR_VALUE


def day_number_for(d: date) -> int:
    """Convert a calendar date to LoseIt's internal day number."""
    anchor = datetime.strptime(DAY_NUM_ANCHOR_DATE, "%Y-%m-%d").date()
    return DAY_NUM_ANCHOR_VALUE + (d - anchor).days


def parse_date_arg(s: str | None) -> date:
    """Parse ``YYYY-MM-DD`` or return today's date if ``s`` is falsy."""
    if not s:
        return datetime.now().date()
    return datetime.strptime(s, "%Y-%m-%d").date()
