"""Conformance tests for ``FoodLogEntry.created_at`` / ``modified_at``.

Spec: ``docs/backup-spec.md`` §4.1 (the grain entry has both timestamps)
and §4.4 (upsert match key uses ``created_at`` ± 10 minutes). The
SDK-side surface is documented in ``docs/backup-impl-plan.md`` §3 / §6
(T4 scenarios).

These exist to:

- Pin the wire-shape extraction (FLE.f4 / FLE.f5 epoch-ms longs ->
  aware UTC ``datetime`` on the dataclass) against the captured fixture.
- Pin the JSON projection (``to_dict()`` emits ISO 8601 with ``+00:00``).
- Guard the corpus: the new fields default to ``None`` so existing
  tests can still build ``FoodLogEntry`` without supplying timestamps.
"""

from __future__ import annotations

import re
from datetime import date

from lose_it.core import daily
from lose_it.models import FoodLogEntry

SERVICE_URL = "https://www.loseit.com/web/service"

# ``to_dict`` emits ``datetime.isoformat()`` output — always carries a
# ``+00:00`` suffix because we build aware UTC instances. The optional
# fractional-second group is here because some FLE timestamps land
# on whole seconds and some don't; both shapes are valid ISO 8601.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")


def test_food_log_entry_has_created_at_after_decode(test_client, httpx_mock, fixture_text):
    """Every decoded FLE in the captured fixture exposes both timestamps.

    The fixture is a real ``getDailyDetailsIncludingPendingForDate``
    response with five FLEs, each carrying non-null ``f4``/``f5``
    longs. After decode they must surface as aware UTC ``datetime``
    instances — UTC because Lose It! stores wall-clock UTC server-side
    and the spec's upsert join key depends on it (§4.4).
    """
    # The SDK first hits ``getInitializationData`` to resolve the day_key,
    # then issues the daily-details RPC. Stub both in order.
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_initialization_data.txt"),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_with_user_saved_food.txt"),
    )

    entries = daily.get_daily_details(test_client.http, date(2026, 6, 12))
    assert entries, "expected the fixture to contain at least one FLE"

    for e in entries:
        assert e.created_at is not None, (
            f"created_at missing for {e.food_name!r}; spec requires it"
        )
        assert e.created_at.tzinfo is not None, (
            f"created_at must be tz-aware (got naive) for {e.food_name!r}"
        )
        # Server stores UTC; the SDK must surface UTC (offset 0) so the
        # restore-mode upsert key (food_id, created_at ± 10m) is a real
        # equality and not subject to local-tz drift.
        utcoff = e.created_at.utcoffset()
        assert utcoff is not None and utcoff.total_seconds() == 0, (
            f"created_at offset must be UTC; got {utcoff} for {e.food_name!r}"
        )

        # Modified is also surfaced — equals ``created_at`` on entries
        # the user never edited; can be later for edited entries. The
        # spec needs it for diff review even though it's not in the
        # upsert key.
        assert e.modified_at is not None, (
            f"modified_at missing for {e.food_name!r}; spec requires it"
        )


def test_to_dict_iso_format(test_client, httpx_mock, fixture_text):
    """``FoodLogEntry.to_dict()`` emits ISO 8601 strings with ``+00:00``.

    This is the projection ``loseit diary --output json`` consumes and
    what the backup grain writer (T1/T2) will land on disk. The spec
    §4.1 sample shows ``2019-08-14T12:34:08+00:00`` — we pin that
    shape so downstream readers (jq, the restore loader) can parse
    timestamps without provider-specific affordances.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_initialization_data.txt"),
    )
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_daily_details_with_user_saved_food.txt"),
    )

    entries = daily.get_daily_details(test_client.http, date(2026, 6, 12))
    assert entries

    payload = entries[0].to_dict()
    assert "created_at" in payload, "to_dict must surface created_at"
    assert "modified_at" in payload, "to_dict must surface modified_at"
    assert payload["created_at"] is not None
    assert payload["modified_at"] is not None
    assert _ISO_RE.match(payload["created_at"]), (
        f"created_at not ISO-8601 UTC: {payload['created_at']!r}"
    )
    assert _ISO_RE.match(payload["modified_at"]), (
        f"modified_at not ISO-8601 UTC: {payload['modified_at']!r}"
    )


def test_food_log_entry_constructible_without_timestamps():
    """Existing test corpus still builds ``FoodLogEntry`` without timestamps.

    The new fields are defaulted to ``None`` and live at the end of the
    dataclass so positional / pre-existing keyword constructions keep
    working. This is the regression guard.
    """
    e = FoodLogEntry(
        food_category="C",
        food_name="N",
        food_brand="B",
        food_pk_response=[],
        entry_pk_response=[],
        entry_day_key="",
        context_day_key="",
        day_num=0,
        hours_from_gmt=0,
        meal_ordinal=0,
        extra_ordinal=3,
        food_measure_ordinal=0,
        servings=1.0,
        food_identifier_code="",
    )
    assert e.created_at is None
    assert e.modified_at is None
