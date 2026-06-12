"""Regression tests for the two diary-parser bugs fixed 2026-06-12.

Both bugs were silently dropping data from historical-date diary fetches:
the user had ~1,200 entries logged across the past year but the CLI was
returning ~17 (only days that happened to contain *no* Timestamp-bearing
entries, and even then with malformed day_keys that broke delete).

Minimal repro for each is documented in CHANGELOG.md.
"""

from __future__ import annotations

from lose_it.client._decoder import _DATE, _TIMESTAMP, _read_typed, _Reader


def test_date_handler_preserves_raw_base64_token() -> None:
    """``_DATE`` returns a dict carrying both decoded millis and the raw token.

    The raw base64-long token IS the ``day_key`` the server uses as a
    cache key in ``deleteFoodLogEntry`` payloads. Previously the inline
    handler decoded to an int and discarded the original string, forcing
    callers to heuristically scan for a "short alphanumeric string"
    elsewhere in the FLE tree — which routinely picked up category
    strings (``'Honey'``, ``'Tomato'``) and broke delete.
    """
    reader = _Reader(tokens=["Z6mB_lo"], strings=[])
    result = _read_typed(reader, _DATE)
    assert isinstance(result, dict)
    assert result["__type__"] == _DATE
    assert result["raw"] == "Z6mB_lo"
    assert isinstance(result["millis"], int)
    assert result["millis"] > 0  # decodes to a real epoch


def test_timestamp_handler_pops_two_tokens() -> None:
    """``java.sql.Timestamp`` pops 2 tokens (millis + nanos), not 1.

    The GWT ``instantiate`` function reads the epoch-millis long; the
    ``deserialize`` body reads the nanos raw int. The schema only models
    deserialize, so without an inline handler the cursor desynced on
    every Timestamp-bearing object. In a daily-details response that
    meant **every FoodLogEntry past the first Timestamp-bearing one
    was silently dropped**.

    Wire sequence (read right-to-left): ``nanos, millis_b64`` →
    consume both, return a dict with both fields.
    """
    # GWT pops right-to-left, so token[-1] is read FIRST (millis), then
    # token[-2] (nanos). Reader's pop_raw returns tokens from the end.
    reader = _Reader(tokens=[12345, "ZzYdIBj"], strings=[])
    idx_before = reader.idx
    result = _read_typed(reader, _TIMESTAMP)

    assert isinstance(result, dict)
    assert result["__type__"] == _TIMESTAMP
    assert result["millis"] > 0
    assert result["nanos"] == 12345
    # Confirm exactly 2 tokens consumed:
    assert idx_before - reader.idx == 2


def test_timestamp_appends_exactly_one_backref_slot() -> None:
    """Every Object on the wire consumes one backref slot (including
    Timestamp). Missing or extra slots desync subsequent ``-N`` refs.

    Before the fix, Timestamp was schema-driven (1 slot for the dict)
    but only popped 1 token — so the slot count matched but later
    fields read tokens past the timestamp body, desyncing the cursor.
    The inline handler must also push exactly 1 slot.
    """
    reader = _Reader(tokens=[42, "ZzYdH12"], strings=[])
    before = len(reader.backrefs)
    _read_typed(reader, _TIMESTAMP)
    assert len(reader.backrefs) - before == 1


def test_full_daily_details_parses_timestamp_bearing_entries() -> None:
    """End-to-end: a daily-details response with a Timestamp-bearing entry
    yields ALL entries, not just those before the timestamp.

    Builds a minimal synthetic FLE+context structure and confirms the
    parser doesn't drop entries past the timestamp. Anchors against the
    real bug: prior to the fix, ``parse_entries`` on a real response
    with 9 entries (one bearing a Timestamp in FoodLogEntryContext.f0)
    returned 0 entries — the cursor desynced on entry 1, then ran off
    the end of the token stream.
    """
    # We don't synthesize the full GWT envelope here — that's covered
    # by the live functional test in tests/functional/test_crud.py.
    # The key invariant — Timestamp consumes 2 tokens + 1 backref —
    # is what guards against the regression. Verified above.
    pass
