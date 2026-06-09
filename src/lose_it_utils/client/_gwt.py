"""GWT-RPC serialization primitives.

The LoseIt web client uses a Google Web Toolkit (GWT) RPC protocol that
serializes Java objects as pipe-delimited string-and-int tokens, with a
trailing JSON array of unique strings (a "string table"). References to
table entries are 1-indexed integers in the data section.

Two GWT-specific quirks to be aware of:

1. **Byte arrays are reversed.** ``byte[]`` fields are written in reverse
   order on the wire — both directions. So the same UUID PK appears as
   ``[a,b,c,...,p]`` in a request body and ``[p,...,c,b,a]`` in the
   response body. Round-tripping requires :func:`reverse_bytes`.

2. **Object fields are serialized in declaration order.** When you parse a
   response object back, you read its fields back-to-front relative to how
   the JVM wrote them. A ``FoodLogEntry`` containing ``FoodIdentifier``,
   ``Context``, ``PrimaryKey``, … appears in the response stream as the
   inverse sequence: PK, context, identifier, …
"""
from __future__ import annotations

import re
from typing import Iterable


def reverse_bytes(byte_list: Iterable[int]) -> list[int]:
    """Reverse the order of a 16-byte primary-key sequence.

    Used when round-tripping byte[] fields between requests and responses.
    """
    return list(reversed(list(byte_list)))


def parse_response(text: str) -> tuple[list, list[str]]:
    """Parse a ``//OK[…]`` GWT-RPC response into ``(tokens, string_table)``.

    Returns ``([], [])`` for non-OK responses. The data section is split on
    commas; integers and floats are converted; quoted strings are unwrapped.
    String references in the data section are 1-indexed ints into the
    returned ``string_table``.
    """
    if not text or not text.startswith("//OK["):
        return [], []

    inner = text[5:-1]
    table_start = inner.rfind(',["')
    if table_start == -1:
        table_start = inner.rfind(",[")
    if table_start == -1:
        return [], []

    data_str = inner[:table_start]
    table_str = inner[table_start + 1:]

    string_table = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', table_str):
        s = m.group(1).replace("\\u0026", "&").replace('\\"', '"').replace("\\\\", "\\")
        string_table.append(s)

    tokens: list = []
    for tok in data_str.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith('"') and tok.endswith('"'):
            tokens.append(tok[1:-1].replace("\\u0026", "&"))
        else:
            try:
                tokens.append(float(tok) if "." in tok else int(tok))
            except ValueError:
                tokens.append(tok)
    return tokens, string_table


def build_envelope(strings: list[str], data_parts: list[str]) -> str:
    """Build the outer GWT-RPC envelope ``7|0|N|<strings>|<data>|``."""
    header = f"7|0|{len(strings)}|" + "|".join(strings) + "|"
    return header + "|".join(data_parts) + "|"


def resolve_string(string_table: list[str], ref: int) -> str | None:
    """Resolve a 1-indexed string reference; return ``None`` if out of range."""
    if isinstance(ref, int) and 1 <= ref <= len(string_table):
        return string_table[ref - 1]
    return None


def is_short_key(s: object) -> bool:
    """Heuristic: is this token a GWT short string (day_key / food_code)?"""
    return (
        isinstance(s, str)
        and 4 <= len(s) <= 16
        and bool(re.match(r"^[A-Za-z0-9_$]+$", s))
    )


def is_food_identifier_code(s: object) -> bool:
    """Heuristic: LoseIt food IDs always start with ``Do`` (e.g. ``DoAGYj``, ``DoA3$q``).

    The trailing chars are GWT short-string alphabet (letters, digits,
    ``_``, ``$``), not just alphanumerics.
    """
    return isinstance(s, str) and bool(re.match(r"^Do[A-Za-z0-9_$]+$", s))


def fmt_num(v: float) -> str:
    """Render a number for the wire — integer-typed if integer-valued."""
    return str(int(v)) if v == int(v) else str(v)
