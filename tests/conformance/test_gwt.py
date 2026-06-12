"""Unit tests for low-level GWT-RPC helpers."""

from __future__ import annotations

from lose_it.core import _gwt


def test_reverse_bytes_roundtrips() -> None:
    """A double-reverse of a byte list is the identity."""
    bs = [1, -128, 127, 0, -1, 50, -42, 12, 99, 100, 101, -50, -49, -48, -47, -46]
    assert _gwt.reverse_bytes(_gwt.reverse_bytes(bs)) == bs


def test_parse_response_handles_non_ok() -> None:
    """Non-``//OK`` responses parse to empty tokens + empty string table."""
    assert _gwt.parse_response("//EX[…]") == ([], [])
    assert _gwt.parse_response("") == ([], [])


def test_parse_response_extracts_string_table() -> None:
    """A simple ``//OK`` response with a string table parses both sides."""
    text = '//OK[1,2,3,4,5,"hello","world",["alpha","beta"]]'
    _tokens, strings = _gwt.parse_response(text)
    assert "alpha" in strings and "beta" in strings


def test_is_food_identifier_code_recognizes_dollar_signs() -> None:
    """Food identifier codes can contain ``$`` characters (e.g. ``DoA3$q``)."""
    assert _gwt.is_food_identifier_code("DoAGYj")
    assert _gwt.is_food_identifier_code("DoA3$q")
    assert _gwt.is_food_identifier_code("DoA4_x")
    assert not _gwt.is_food_identifier_code("Z6pBKc4")  # day key
    assert not _gwt.is_food_identifier_code("Plain")  # no Do prefix
    assert not _gwt.is_food_identifier_code(0)  # non-string


def test_fmt_num_keeps_integers_integral() -> None:
    """``fmt_num`` returns ``'1'`` for 1.0 but ``'1.5'`` for fractional values."""
    assert _gwt.fmt_num(1.0) == "1"
    assert _gwt.fmt_num(0) == "0"
    assert _gwt.fmt_num(1.5) == "1.5"


def test_build_envelope_format() -> None:
    """The envelope is ``7|0|N|<strings…>|<data…>|`` with correct counts."""
    out = _gwt.build_envelope(["a", "b", "c"], ["1", "2"])
    assert out.startswith("7|0|3|a|b|c|")
    assert out.endswith("|1|2|")
