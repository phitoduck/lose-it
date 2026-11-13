"""Forward reader for GWT-RPC *request* bodies, driven by ``_schemas.json``.

The SDK's decoder (``lose_it.core._decoder``) reads *responses*, which GWT
writes back-to-front. Requests go the other way: the client writes fields
front-to-back in serializer order. This helper walks a request body that
way so tests can assert that a payload the SDK builds is structurally
valid — every token consumed, every object typed — and inspect its
fields by declaration index.

Field indices follow the schema's field order, which is the Java class's
serializer order (e.g. ``Recipe`` field 4 is the serving quantity).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

_SCHEMAS: dict[str, dict[str, Any]] = json.loads(
    files("lose_it.core").joinpath("_schemas.json").read_text()
)["schemas"]
# Serialize-only types: the client bundle has no deserializer for them,
# so the extractor skips them. Field order from their serializers.
_SCHEMAS.setdefault(
    "com.loseit.core.client.service.ServiceRequestToken/1076571655",
    {"fields": ["OBJECT", "OBJECT"], "is_enum": False},
)
_SCHEMAS.setdefault(
    "com.loseit.core.client.model.UserId/4281239478",
    {"fields": ["RAW", "STRING", "RAW"], "is_enum": False},
)


@dataclass
class Obj:
    """A decoded object: short type name + fields by declaration index."""

    type: str
    fields: dict[int, Any] = field(default_factory=dict)

    def __getitem__(self, i: int) -> Any:
        return self.fields[i]


@dataclass
class Enum:
    type: str
    ordinal: int


@dataclass
class BackRef:
    index: int


@dataclass
class ParsedRequest:
    service: str
    method: str
    param_types: list[str]
    params: list[Any]
    tokens_consumed: int
    tokens_total: int


class _Reader:
    def __init__(self, strings: list[str], data: list[str]) -> None:
        self.strings = strings
        self.data = data
        self.pos = 0

    def tok(self) -> str:
        t = self.data[self.pos]
        self.pos += 1
        return t

    def string(self) -> str | None:
        n = int(self.tok())
        return None if n == 0 else self.strings[n - 1]

    def obj(self) -> Any:
        n = int(self.tok())
        if n == 0:
            return None
        if n < 0:
            return BackRef(-n)
        sig = self.strings[n - 1]
        name = sig.split("/")[0]
        short = name.rsplit(".", 1)[-1]
        if name == "java.util.HashMap":
            count = int(self.tok())
            return [(self.obj(), self.obj()) for _ in range(count)]
        if name == "[B":
            count = int(self.tok())
            return [int(self.tok()) for _ in range(count)]
        if name == "java.util.Date":
            return ("Date", self.tok())
        if name == "java.sql.Timestamp":
            return ("Timestamp", self.tok(), self.tok())
        if name == "java.lang.Double":
            return float(self.tok())
        schema = _SCHEMAS.get(sig)
        if schema is None:
            raise KeyError(f"no schema for {sig!r} at token {self.pos}")
        if schema["is_enum"]:
            return Enum(short, int(self.tok()))
        if name.startswith("[L"):
            count = int(self.tok())
            return [self.obj() for _ in range(count)]
        out = Obj(short)
        for i, kind in enumerate(schema["fields"]):
            if kind == "OBJECT":
                out.fields[i] = self.obj()
            elif kind == "STRING":
                out.fields[i] = self.string()
            else:
                out.fields[i] = self.tok()
        return out


def parse_request(body: str) -> ParsedRequest:
    """Parse a ``7|0|N|<strings>|<data>|`` request body."""
    parts = body.strip().split("|")
    assert parts[0] == "7", "not a GWT-RPC v7 request"
    assert parts[-1] == "", "request body must end with '|'"
    parts = parts[:-1]
    n = int(parts[2])
    strings, data = parts[3 : 3 + n], parts[3 + n :]
    r = _Reader(strings, data)
    r.string()  # module base URL
    r.string()  # policy hash
    service = r.string() or ""
    method = r.string() or ""
    n_params = int(r.tok())
    param_types = [r.string() or "" for _ in range(n_params)]
    params = [r.obj() for _ in range(n_params)]
    return ParsedRequest(service, method, param_types, params, r.pos, len(data))
