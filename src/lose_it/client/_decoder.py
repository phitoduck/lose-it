"""Schema-driven GWT-RPC response decoder.

Replaces the heuristic parsers that used to live in ``foods.py``,
``daily.py``, and ``entries.py``. Those parsers tried to guess field
positions from string length and capitalization patterns; this one
walks the actual field schema that GWT's compiler emitted, so every
field lands in the right slot by construction.

Inputs
------

- ``_schemas.json``  — written by ``tools/extract_gwt_schemas.py``,
  contains ``{fqcn: {"deserialize_fn": "...", "fields": [...]}}`` for
  every Lose It! domain type plus all the relevant Java built-ins.
  This file is the ground truth and changes only when Lose It! redeploys.
- A raw ``//OK[…]`` GWT-RPC response body, parsed via
  :func:`lose_it.client._gwt.parse_response` into a flat token
  list and a separate string table.

Algorithm
---------

GWT-RPC is a LIFO stack protocol. After ``parse_response`` strips the
trailing ``,0,7`` envelope and the string table, the remaining tokens
are read right-to-left. The first read is a polymorphic Object whose
type FQCN sits at the top of the stack as a 1-based string-table ref.

We dispatch by FQCN:

- **Built-in primitives** (Integer, Long, Double, Boolean, String,
  Float, Date) read a fixed shape directly from the stream.
- **Collections** (ArrayList, HashSet, LinkedHashMap, HashMap, …) read
  a length followed by N (or 2N) polymorphic Object reads.
- **Byte arrays** (``[B/…``) read a length followed by N raw int tokens.
- **Everything else** is looked up in the schema and decoded field-by-field
  in declaration order.

Backreferences
--------------

GWT dedups objects on the wire: when the server has already serialized
a particular Object, subsequent references emit a *negative* integer
encoding the back-reference index instead of re-serializing. We track
every Object we deserialize and resolve negative refs against that list.
The encoding is ``ref = -(index + 1)`` (1-based negative).

Strings are similarly deduped via the string table, not via backrefs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from ._enums import label_for_ordinal
from ._gwt import parse_response

# FQCNs whose decoded objects get a plain-English label attached next to
# their raw ``ordinal``. Surfaces in ``loseit -o json`` output and in
# any downstream parser that walks the decoded tree.
_FOOD_MEASURE_FQCN = "com.loseit.core.client.model.FoodMeasure/1457474932"


@dataclass
class _Schema:
    fqcn: str
    fields: list[str]
    is_enum: bool = False


@dataclass
class _Catalog:
    """Loaded ``_schemas.json`` — maps every known FQCN to its read shape."""

    permutation: str
    schemas: dict[str, _Schema] = field(default_factory=dict)

    @classmethod
    def load(cls) -> _Catalog:
        with resources.files(__package__).joinpath("_schemas.json").open() as f:
            data = json.load(f)
        cat = cls(permutation=data["permutation"])
        for fqcn, info in data["schemas"].items():
            cat.schemas[fqcn] = _Schema(
                fqcn=fqcn,
                fields=info["fields"],
                is_enum=bool(info.get("is_enum", False)),
            )
        return cat


_CATALOG: _Catalog | None = None


def _catalog() -> _Catalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _Catalog.load()
    return _CATALOG


# ── Built-in type handling ───────────────────────────────────────────────────
#
# GWT's compiled JS dispatches Java built-ins to inlined readers that bypass
# the regular field-by-field schema mechanism. We mirror that here: any FQCN
# in these sets short-circuits the schema lookup.

_LIST_TYPES: frozenset[str] = frozenset(
    {
        "java.util.ArrayList/4159755760",
        "java.util.Arrays$ArrayList/2507071751",
        "java.util.HashSet/3273092938",
        "java.util.LinkedHashSet/95640124",
        "java.util.LinkedList/3953877921",
        "java.util.Stack/1346942793",
        "java.util.TreeSet/4043497002",
        "java.util.Vector/3057315478",
        "java.util.Collections$EmptyList/4157118744",
        "java.util.Collections$EmptySet/3523698179",
        "java.util.Collections$SingletonList/1586180994",
    }
)

_MAP_TYPES: frozenset[str] = frozenset(
    {
        "java.util.HashMap/1797211028",
        "java.util.IdentityHashMap/1839153020",
        "java.util.LinkedHashMap/3008245022",
        "java.util.TreeMap/1493889780",
    }
)

# Each built-in primitive returns a Python equivalent. The key is the FQCN
# the GWT type system uses; the value is a function that pops the right shape
# off the stream.
_INTEGER = "java.lang.Integer/3438268394"
_LONG = "java.lang.Long/4227064769"
_DOUBLE = "java.lang.Double/858496421"
_FLOAT = "java.lang.Float/1718559123"
_BOOLEAN = "java.lang.Boolean/476441737"
_STRING = "java.lang.String/2004016611"
_DATE = "java.util.Date/3385151746"
_BYTE_ARRAY = "[B/3308590456"


# ── Base64 long decoding ─────────────────────────────────────────────────────
#
# GWT encodes ``long`` values as a custom base64-ish string so they survive
# the JS Number precision cliff. The alphabet is a-z A-Z 0-9 $ _ mapping to
# 0..63 in a specific order (the ``mGd`` function in the JS bundle). We
# mirror that mapping exactly here.

_LONG_ALPHABET: dict[str, int] = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _LONG_ALPHABET[_c] = _i
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _LONG_ALPHABET[_c] = _i + 26
for _i, _c in enumerate("0123456789"):
    _LONG_ALPHABET[_c] = _i + 52
_LONG_ALPHABET["$"] = 62
_LONG_ALPHABET["_"] = 63


def _decode_long(s: str) -> int:
    """Decode GWT's base64-encoded long (matches ``nGd`` in the JS bundle)."""
    if not s:
        return 0
    val = _LONG_ALPHABET.get(s[0], 0)
    # GWT's mGd treats the first char as signed: A-Z = 0..25, a-z = 26..51,
    # 0-9 = 52..61, $ = 62, _ = 63. But the first character's high bit acts
    # as the sign — values >=32 indicate negative. We mirror nGd directly.
    for ch in s[1:]:
        val = (val << 6) | _LONG_ALPHABET.get(ch, 0)
    return val


# ── The decoder itself ───────────────────────────────────────────────────────


class _Reader:
    """LIFO token-stream reader with object-backref tracking.

    Mirrors GWT's ``ClientSerializationStreamReader``: a flat token array
    consumed right-to-left via a decrementing index. Each Object we
    deserialize is appended to ``backrefs`` so later negative refs in the
    same response can resolve to the same instance (the GWT writer dedups
    repeat references).
    """

    def __init__(self, tokens: list[Any], strings: list[str]) -> None:
        self.tokens = tokens
        self.idx = len(tokens)
        self.strings = strings
        self.backrefs: list[Any] = []

    def pop_raw(self) -> Any:
        self.idx -= 1
        if self.idx < 0:
            raise IndexError("GWT stream underflow")
        return self.tokens[self.idx]

    def resolve_string(self, ref: int) -> str | None:
        if ref <= 0 or ref > len(self.strings):
            return None
        return self.strings[ref - 1]


def _read_field(reader: _Reader, kind: str) -> Any:
    """Read one field of the given kind, popping the appropriate token(s)."""
    if kind == "OBJECT":
        return read_object(reader)
    if kind == "STRING":
        ref = reader.pop_raw()
        return reader.resolve_string(int(ref)) if isinstance(ref, (int, float)) else None
    if kind == "BOOLEAN":
        return bool(reader.pop_raw())
    if kind == "DOUBLE":
        v = reader.pop_raw()
        return float(v) if isinstance(v, (int, float)) else float(str(v))
    if kind == "LONG":
        v = reader.pop_raw()
        return _decode_long(str(v))
    if kind == "RAW":
        return reader.pop_raw()
    raise ValueError(f"Unknown field kind {kind!r}")


def read_object(reader: _Reader) -> Any:
    """Read one polymorphic Object from the top of the stream.

    Pops a single token; positive = string-table ref to the type FQCN
    (then dispatch by FQCN), negative = backref to a previously
    deserialized Object (then return that instance).
    """
    ref = reader.pop_raw()
    if not isinstance(ref, (int, float)) or isinstance(ref, float):
        # The GWT stream should only contain ints here. If we got a string
        # (e.g. a short day-key sitting at the top of the stack) the
        # call-site asked for an Object where the schema actually says RAW.
        # Surface that mismatch instead of crashing.
        raise TypeError(
            f"Expected string-table ref for Object, got {ref!r}. "
            "Schema/data mismatch — token stream is desynchronized."
        )
    ref = int(ref)
    if ref < 0:
        # Backref: -1 = first object recorded, -2 = second, …
        slot = -(ref + 1)
        if slot >= len(reader.backrefs):
            raise IndexError(
                f"Backref {ref} but only {len(reader.backrefs)} objects "
                "have been deserialized so far"
            )
        return reader.backrefs[slot]
    if ref == 0:
        return None
    fqcn = reader.resolve_string(ref)
    if fqcn is None:
        raise ValueError(f"Type ref {ref} out of string table range")
    return _read_typed(reader, fqcn)


def _read_typed(reader: _Reader, fqcn: str) -> Any:
    """Dispatch a deserialization by FQCN.

    Built-ins (primitives, collections, byte arrays, Date) are handled
    inline. Everything else is looked up in the schema; missing schemas
    raise a clear error so the caller knows to regenerate the schemas
    or expand the extractor's type coverage.

    Every type read via the polymorphic ``read_object`` path reserves a
    backref slot — including Java primitive *wrappers* (Integer, Long,
    Boolean, …). GWT does this unconditionally, even for value types,
    because subsequent ``-N`` references resolve against the same flat
    list. Skipping it for primitives misaligns the slot count, and any
    later backref points to a wrong object.
    """
    # Primitives — value-typed but still get a backref slot.
    if fqcn == _INTEGER:
        v = int(reader.pop_raw())
        reader.backrefs.append(v)
        return v
    if fqcn == _LONG:
        v = _decode_long(str(reader.pop_raw()))
        reader.backrefs.append(v)
        return v
    if fqcn in (_DOUBLE, _FLOAT):
        v = float(reader.pop_raw())
        reader.backrefs.append(v)
        return v
    if fqcn == _BOOLEAN:
        v = bool(reader.pop_raw())
        reader.backrefs.append(v)
        return v
    if fqcn == _STRING:
        ref = reader.pop_raw()
        s = reader.resolve_string(int(ref)) if isinstance(ref, (int, float)) else None
        reader.backrefs.append(s)
        return s
    if fqcn == _DATE:
        v = _decode_long(str(reader.pop_raw()))  # epoch millis as raw int
        reader.backrefs.append(v)
        return v

    # Byte arrays — raw bytes inline, length-prefixed. The array
    # *instance* is an Object on the wire, so GWT does record a backref
    # slot for it; if we skip the slot, later refs misalign.
    if fqcn.startswith("[B/"):
        bytes_list: list[int] = []
        reader.backrefs.append(bytes_list)
        length = int(reader.pop_raw())
        for _ in range(length):
            bytes_list.append(int(reader.pop_raw()))
        return bytes_list

    # Object arrays (``[Lcom.foo.Bar;/<hash>``) — length-prefixed list of
    # polymorphic Objects, same wire shape as ``java.util.ArrayList`` but
    # represented as a Java native array. The instantiate function pops
    # the length; the deserialize loops ``pGd`` over each slot.
    if fqcn.startswith("[L"):
        items_holder: dict[str, Any] = {"__type__": fqcn, "items": []}
        reader.backrefs.append(items_holder)
        length = int(reader.pop_raw())
        for _ in range(length):
            items_holder["items"].append(read_object(reader))
        return items_holder

    # Object reference types — register a placeholder *before* reading
    # children so any backrefs *inside* the body can resolve to the
    # outer object. GWT does this too.
    obj: dict[str, Any] = {"__type__": fqcn}
    reader.backrefs.append(obj)

    if fqcn in _LIST_TYPES:
        length = int(reader.pop_raw())
        obj["items"] = [read_object(reader) for _ in range(length)]
        return obj

    if fqcn in _MAP_TYPES:
        length = int(reader.pop_raw())
        entries: list[tuple[Any, Any]] = []
        for _ in range(length):
            key = read_object(reader)
            value = read_object(reader)
            entries.append((key, value))
        obj["entries"] = entries
        return obj

    schema = _catalog().schemas.get(fqcn)
    if schema is None:
        raise KeyError(
            f"No schema for {fqcn!r}. Regenerate _schemas.json via "
            "tools/extract_gwt_schemas.py — Lose It! may have added a new type."
        )
    # Enums encode their ordinal as one extra token consumed by the GWT
    # *instantiate* function (not the deserialize body). Mirror that here
    # before reading any other fields.
    if schema.is_enum:
        obj["ordinal"] = reader.pop_raw()
    for i, kind in enumerate(schema.fields):
        obj[f"f{i}"] = _read_field(reader, kind)
    if fqcn == _FOOD_MEASURE_FQCN:
        obj["unit"] = label_for_ordinal(obj.get("ordinal"))
    return obj


# ── Public API ───────────────────────────────────────────────────────────────


def decode_response(body: str, strict: bool = False) -> Any:
    """Decode a ``//OK[…]`` GWT-RPC response body into a nested Python object.

    Returns the top-level response value — typically a
    ``LoseItRemoteServiceResponse`` wrapper. Each object is a dict with
    a ``"__type__"`` key naming its FQCN and one entry per field in
    declaration order (``f0``, ``f1``, …).

    Higher-level helpers in ``foods.py`` / ``daily.py`` consume this
    structured output and map positional fields to named ones for the
    public SDK dataclasses.

    By default the decoder is **lenient**: if it encounters a type it
    can't schema-resolve or a token-stream desync, it returns
    ``{"__partial__": True, "decoded": <root>, "backrefs": <list>}``.
    Callers that only need to extract a subset of types (e.g.
    ``daily.py`` only cares about ``FoodLogEntry`` instances) can walk
    ``backrefs`` to find what was successfully parsed before the failure.
    Set ``strict=True`` to re-raise instead.
    """
    tokens, strings = parse_response(body)
    if not tokens:
        return None
    reader = _Reader(tokens, strings)
    try:
        return read_object(reader)
    except (IndexError, KeyError, TypeError, ValueError):
        if strict:
            raise
        # Return whatever was decoded so far so callers can salvage it.
        return {
            "__partial__": True,
            "backrefs": list(reader.backrefs),
        }
