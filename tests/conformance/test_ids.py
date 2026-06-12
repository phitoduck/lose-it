"""Conformance tests for ``_ids.pk_to_hex`` / ``_ids.hex_to_pk``.

Round-trips 16-byte SimplePrimaryKey values between the SDK's "response
form" (``list[int]`` in [-128, 127]) and 32-char lowercase hex.
"""

from __future__ import annotations

import random

import pytest

from lose_it.client._ids import hex_to_pk, pk_to_hex

# A known PK from the spec's wire-evidence snippet. The 16 bytes appear
# at the end of the data section in reversed (wire) form; ``pk_bytes``
# (response form) is the un-reversed view.
#
# wire bytes: 16, 17, 13, 95, 48, -65, 66, 49, -93, -38, -116, 60, -106, 8, -16, 99
# response-form = reversed(wire):
_SPEC_PK_RESPONSE_FORM = [99, -16, 8, -106, 60, -116, -38, -93, 49, 66, -65, 48, 95, 13, 17, 16]
# Verify by hand: 99 -> 0x63, -16 -> 0xf0, 8 -> 08, -106 -> 0x96, 60 -> 0x3c,
# -116 -> 0x8c, -38 -> 0xda, -93 -> 0xa3, 49 -> 0x31, 66 -> 0x42, -65 -> 0xbf,
# 48 -> 0x30, 95 -> 0x5f, 13 -> 0x0d, 17 -> 0x11, 16 -> 0x10
_SPEC_EXPECTED_HEX = "63f008963c8cdaa33142bf305f0d1110"


def test_pk_to_hex_known_fixture() -> None:
    """Encoding the spec's PK matches the expected 32-char lowercase hex."""
    assert pk_to_hex(_SPEC_PK_RESPONSE_FORM) == _SPEC_EXPECTED_HEX


def test_hex_to_pk_known_fixture() -> None:
    """Decoding the expected hex round-trips back to the spec's PK bytes."""
    assert hex_to_pk(_SPEC_EXPECTED_HEX) == _SPEC_PK_RESPONSE_FORM


def test_pk_to_hex_is_lowercase_and_32_chars() -> None:
    pk = [-128, -1, 0, 1, 127, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    out = pk_to_hex(pk)
    assert len(out) == 32
    assert out == out.lower()
    # high byte (-128) -> 0x80, (-1) -> 0xff, 0 -> 00, 1 -> 01, 127 -> 7f
    assert out.startswith("80ff00017f")


def test_hex_to_pk_accepts_uppercase() -> None:
    lower = "9eba9129b8494967c8cb3385acf0f614"
    upper = lower.upper()
    assert hex_to_pk(upper) == hex_to_pk(lower)


def test_hex_to_pk_strips_whitespace() -> None:
    lower = "9eba9129b8494967c8cb3385acf0f614"
    assert hex_to_pk(f"  {lower}\n") == hex_to_pk(lower)


def test_hex_to_pk_rejects_non_hex() -> None:
    with pytest.raises(ValueError, match="not valid hex"):
        hex_to_pk("not-hex-at-all-32-chars-padding!")


def test_hex_to_pk_rejects_short() -> None:
    with pytest.raises(ValueError, match="32 hex chars"):
        hex_to_pk("9eba")


def test_hex_to_pk_rejects_long() -> None:
    with pytest.raises(ValueError, match="32 hex chars"):
        hex_to_pk("9eba9129b8494967c8cb3385acf0f614ff")


def test_pk_to_hex_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        pk_to_hex([1, 2, 3])


def test_round_trip_random_pks() -> None:
    """Round-trip 1000 random 16-byte PKs through both encoders."""
    rng = random.Random(0xC0FFEE)
    for _ in range(1000):
        pk = [rng.randint(-128, 127) for _ in range(16)]
        hexed = pk_to_hex(pk)
        assert len(hexed) == 32
        assert hex_to_pk(hexed) == pk


def test_round_trip_via_hex_first() -> None:
    """Going hex -> PK -> hex is also a no-op."""
    rng = random.Random(0xBEEF)
    for _ in range(100):
        raw = bytes(rng.randint(0, 255) for _ in range(16))
        hexed = raw.hex()
        assert pk_to_hex(hex_to_pk(hexed)) == hexed
