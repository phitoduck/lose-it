"""Live-API end-to-end CRUD test for the Lose It! SDK.

This test:

1. Searches the food DB.
2. Picks a result and fetches its ``getUnsavedFoodLogEntry`` template.
3. Logs it to today's snacks.
4. Reads back today's diary and verifies the entry is there.
5. Deletes the entry.
6. Reads back the diary and verifies the entry is gone.

The raw GWT-RPC response bodies are written to ``tests/conformance/fixtures/``
along the way; the conformance/unit tests read them back as mock responses.
The fixtures are sanitized via ``tests/_sanitize.py`` so checking them into a
public repo doesn't leak the real account's user id / username.

Skipped unless ``LOSEIT_RUN_FUNCTIONAL=1``. Required env: ``LOSEIT_USER_ID``,
``LOSEIT_USER_NAME``, ``LOSEIT_POLICY_HASH``, ``LOSEIT_STRONG_NAME``,
``LOSEIT_HOURS_FROM_GMT``, plus a valid token in ``~/.config/loseit/token``.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from lose_it import Client
from lose_it.client import daily, entries, foods
from lose_it.client._dates import day_number_for
from lose_it.client.init import get_daydate_key

from .._sanitize import sanitize

pytestmark = pytest.mark.functional


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "conformance" / "fixtures"


def _write_fixture(name: str, payload: str) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / name).write_text(sanitize(payload))


@pytest.fixture(autouse=True)
def _gate():
    if os.environ.get("LOSEIT_RUN_FUNCTIONAL") != "1":
        pytest.skip("functional test gated on LOSEIT_RUN_FUNCTIONAL=1")


def test_full_crud_round_trip(tmp_path: Path) -> None:
    """End-to-end: search → log → list → delete → list, capturing fixtures."""
    when = date.today()
    query = "x-treme carb balance tortilla"

    with Client.from_env() as client:
        # 1. search
        search_payload_resp = client.http.post_rpc(
            foods._build_search_payload(client.config, query)
        )
        _write_fixture("search_foods_tortilla.txt", search_payload_resp)
        from lose_it.client._gwt import parse_response

        tokens, strings = parse_response(search_payload_resp)
        results = foods._extract_search_results(tokens, strings)
        assert results, "search returned no candidates"

        # Find the Mission Carb Balance tortilla (heuristic match on brand+name).
        target = next(
            (r for r in results if "Mission" in r.brand or "Mission" in r.name),
            results[0],
        )

        # 2. get_unsaved
        unsaved_resp = client.http.post_rpc(foods._build_unsaved_payload(client.config, target))
        _write_fixture("get_unsaved_tortilla.txt", unsaved_resp)
        t, s = parse_response(unsaved_resp)
        unsaved = foods._parse_unsaved_response(t, s)
        assert unsaved.food_pk_bytes, "unsaved entry missing food PK"

        # 3. log to snacks (so we don't pollute a "real" meal)
        day_num = day_number_for(when)
        day_key = get_daydate_key(client.http, day_num) or ""
        log_payload = entries._build_log_payload(
            client.config,
            unsaved,
            meal_ordinal=3,  # snacks
            day_key=day_key,
            day_num=day_num,
            servings=1.0,
        )
        log_resp = client.http.post_rpc(log_payload)
        _write_fixture("update_food_log_entry_success.txt", log_resp)

        # 4. list — must see our entry
        daily_resp = daily.get_daily_details_raw(client.http, when)
        _write_fixture("get_daily_details_with_tortilla.txt", daily_resp)
        es = daily.parse_entries(daily_resp, default_hours_from_gmt=client.config.hours_from_gmt)
        snacks = [e for e in es if e.meal_ordinal == 3 and "ortilla" in e.food_name]
        assert snacks, "logged tortilla didn't show up in snacks"
        logged_entry = snacks[-1]  # most recent if duplicates

        # 5. delete
        delete_payload = entries._build_delete_payload(client.config, logged_entry)
        delete_resp = client.http.post_rpc(delete_payload)
        _write_fixture("delete_food_log_entry_success.txt", delete_resp)

        # 6. list — must be gone
        daily_after = daily.get_daily_details_raw(client.http, when)
        _write_fixture("get_daily_details_after_delete.txt", daily_after)
        es_after = daily.parse_entries(
            daily_after, default_hours_from_gmt=client.config.hours_from_gmt
        )
        snacks_after = [
            e
            for e in es_after
            if e.meal_ordinal == 3
            and "ortilla" in e.food_name
            and e.entry_pk_response == logged_entry.entry_pk_response
        ]
        assert not snacks_after, "tortilla was not deleted"

        # also capture an init data response for completeness
        from lose_it.client.init import build_payload as build_init_payload

        init_resp = client.http.post_rpc(build_init_payload(client.config))
        _write_fixture("get_initialization_data.txt", init_resp)
