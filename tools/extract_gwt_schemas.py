"""Extract GWT TypeSerializer schemas from Lose It!'s compiled JS bundles.

This is a one-shot tool. Run it after Lose It! redeploys (i.e. when the
``x-gwt-permutation`` changes). It downloads the main ``*.cache.js``
bundle and every loaded ``deferredjs/*.cache.js`` fragment from
``d3hsih69yn4d89.cloudfront.net``, walks the GWT serializer table, and
emits ``src/lose_it/client/_schemas.json`` containing the field
sequence for every Lose It! domain type.

The schema is the **ground truth** for what the server can serialize.
The heuristic parsers in ``foods.py`` / ``daily.py`` previously had to
guess field positions; with the schema, the decoder reads them in the
exact order the Java class declared them.

Usage::

    python tools/extract_gwt_schemas.py \\
        --permutation 351AE5DC0CA36AD3BA9C7CBA7B0E07B8 \\
        --fragments 1 8 10 \\
        --out src/lose_it/client/_schemas.json

The default fragment list is what the live web app loads on first nav;
inspect Chrome DevTools' Network tab if Lose It! starts loading more.
"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://d3hsih69yn4d89.cloudfront.net/web"
DEFAULT_FRAGMENTS = (1, 8, 10)

# GWT read primitives, looked up in the bundle to confirm semantics.
# These names are stable across permutations because they're emitted by
# the GWT compiler from fixed templates. The *meaning* of each function
# is what we care about; the function-name letters change per build.
#
# Pattern → (primitive_kind, comment)
#
# - "<sQ>(<pGd>(a), <id>)"    — read polymorphic Object; cast asserts type id
# - "<wGd>(a, a.b[--a.a])"    — pop next token as ref, look up in string table → String
# - "<yGd>(a)"                — read boolean (`!!a.b[--a.a]`)
# - "<zGd>(a)" / "<AGd>(a)"   — read double (Number(...))
# - "<BGd>(a)"                — read long (base64-encoded; ``nGd`` decodes)
# - "a.b[--a.a]"              — bare token pop; raw int/byte
#
# We resolve the actual letter-names per bundle by inspecting one known
# deserializer (SearchResultFood's reader), since its pattern always
# uses every relevant primitive.
PRIMITIVES = {
    "object": "OBJECT",  # polymorphic — resolved via string-table FQCN ref
    "string": "STRING",
    "boolean": "BOOLEAN",
    "double": "DOUBLE",
    "long": "LONG",
    "raw": "RAW",
}


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def decode_fragment(text: str) -> str | None:
    """Unwrap a ``$wnd.web.runAsyncCallbackN("...")`` wrapper around a JS string."""
    m = re.match(r'\$wnd\.web\.runAsyncCallback\d+\(["\'](.*)["\']\)\s*;?\s*$', text, re.DOTALL)
    if not m:
        return None
    return codecs.decode(m.group(1), "unicode_escape")


def extract_fn_body(text: str, name: str) -> str | None:
    """Find ``function NAME(...) { ... }`` and return the whole block."""
    pat = re.compile(rf"function {re.escape(name)}\(([^)]*)\)\s*\{{")
    m = pat.search(text)
    if not m:
        return None
    depth = 1
    i = m.end()
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return text[m.start() : i].replace("\\n", "\n")


def collect_class_constants(js: str) -> dict[str, str]:
    """Return ``{varname: 'fqcn/hash'}`` for every GWT type constant.

    Captures both Lose It! types (``com.loseit.*``) and Java/GWT built-ins
    (``java.lang.Integer``, ``java.util.ArrayList``, etc.) since GWT-RPC
    responses serialize Java built-ins polymorphically too.
    """
    # Identifier may start with ``$``, which ``\b`` (word boundary) won't
    # anchor against. Use a negative lookbehind for word chars instead, so
    # we capture ``$cw`` as a whole identifier rather than just ``cw``.
    const_re = re.compile(
        r"(?<![A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"'((?:com\.loseit|com\.google|java\.|javax\.|\[(?:com\.loseit|com\.google|java\.|L))[^']+/\d+)'"
    )
    out: dict[str, str] = {}
    for m in const_re.finditer(js):
        out[m.group(1)] = m.group(2)
    return out


def collect_serializer_table(js: str) -> dict[str, tuple[str, str, str | None]]:
    """Return ``{varname: (instantiate_fn, deserialize_fn, serialize_fn?)}``.

    2-element entries ``a[v]=[X,Y]`` are read-only types (server emits,
    client receives). 3-element entries ``a[v]=[X,Y,Z]`` are read+write
    (client both sends and receives). We capture the deserializer either
    way; the serializer is recorded when present for future use.
    """
    pat = re.compile(
        r"(?<![A-Za-z0-9_$])a\[([A-Za-z_$][A-Za-z0-9_$]*)\]\s*=\s*"
        r"\[([A-Za-z0-9_$]+)\s*,\s*([A-Za-z0-9_$]+)(?:\s*,\s*([A-Za-z0-9_$]+))?\]"
    )
    out: dict[str, tuple[str, str, str | None]] = {}
    for m in pat.finditer(js):
        varname, fn1, fn2, fn3 = m.group(1), m.group(2), m.group(3), m.group(4)
        out[varname] = (fn1, fn2, fn3)
    return out


def discover_primitive_names(js: str) -> dict[str, str]:
    """Identify the per-bundle names of the GWT read primitives.

    We do this by finding one *known* deserializer
    (``SearchResultFood``) and observing which letter-names appear in
    canonical positions. Specifically:

    - The wrapper around object reads is ``sQ`` (cast assertion) — its
      function body matches ``oSv(a==null||rQ(a,b))``.
    - The polymorphic-object read function ``pGd`` always pops the token
      stream and calls into a serializer lookup; body contains ``ewd``.
    - The string-resolve helper ``wGd`` returns ``a>0?b.d[a-1]:null``.
    - Boolean reader ``yGd`` body uses ``!!a.b[--a.a]``.
    - Double/Number readers use ``Number(a.b[--a.a])``.
    - Long reader ``BGd`` body uses the base64 helper ``nGd``.
    """

    def find_fn_by_body(snippet: str) -> str | None:
        """Find the function whose body contains ``snippet``.

        Brace-balances forward from each ``function NAME(...)`` until we
        either hit the matching close (skip this candidate) or see the
        snippet inside the body (this one wins).
        """
        for header in re.finditer(r"function ([A-Za-z_$][A-Za-z0-9_$]*)\([^)]*\)\s*\{", js):
            name = header.group(1)
            depth = 1
            i = header.end()
            while i < len(js) and depth > 0:
                c = js[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
            body = js[header.end() : i - 1]
            if snippet in body:
                return name
        return None

    names = {
        "cast_assert": find_fn_by_body("oSv(a==null||rQ(a,b))"),
        "read_object": find_fn_by_body("ewd(a.c,a,c)"),
        "read_string": find_fn_by_body("a>0?b.d[a-1]:null"),
        "read_boolean": find_fn_by_body("b=!!a.b[--a.a]"),
        "read_double": find_fn_by_body("b=Number(a.b[--a.a])"),
        "read_long": find_fn_by_body("c=nGd(a)"),
    }
    missing = [k for k, v in names.items() if v is None]
    if missing:
        raise RuntimeError(f"Could not locate read primitives: {missing}")
    return names  # type: ignore[return-value]


def parse_deserializer_body(
    body: str,
    primitives: dict[str, str],
    all_js: str,
    chain_cache: dict[str, list[str]],
) -> list[str]:
    """Return the sequence of field-read kinds from a deserializer body.

    Each statement in a GWT deserializer is one of:

    - A setter call ``setterFn(b, <read>)`` — reads one field; the kind
      is determined by ``<read>``.
    - A direct field assignment ``b.X = <read>`` — same.
    - A **superclass deserialize call** ``superFn(a, b)`` — the literal
      tokens ``(a, b)`` with that exact argument order are how the
      compiler emits an inlined chain to the parent class. We recurse
      into ``superFn``'s body and splice its field list inline. GWT
      writes superclass fields *before* subclass fields, and the
      compiled deserializer mirrors that order by calling super FIRST or
      LAST depending on which side of the call comes first in source.

    Order of fields matters: each statement appends to ``fields`` in the
    same order it appears in the source. A statement that is neither a
    field read nor a super call (e.g. GWT bookkeeping ``JSv[c=++KSv]=``)
    is silently skipped.
    """
    cast_fn = primitives["cast_assert"]
    obj_fn = primitives["read_object"]
    str_fn = primitives["read_string"]
    bool_fn = primitives["read_boolean"]
    dbl_fn = primitives["read_double"]
    long_fn = primitives["read_long"]

    body = body.replace("\\n", "\n")
    m = re.search(r"\{(.*)\}\s*$", body, re.DOTALL)
    if not m:
        return []
    inner = m.group(1)
    stmts = [s.strip() for s in inner.split(";") if s.strip()]

    cast_obj = re.compile(rf"\b{re.escape(cast_fn)}\(\s*{re.escape(obj_fn)}\(a\)\s*,")
    bare_obj = re.compile(rf"\b{re.escape(obj_fn)}\(a\)")
    str_pat = re.compile(rf"\b{re.escape(str_fn)}\(a,\s*a\.b\[--a\.a\]\)")
    bool_pat = re.compile(rf"\b{re.escape(bool_fn)}\(a\)")
    dbl_pat = re.compile(rf"\b{re.escape(dbl_fn)}\(a\)")
    long_pat = re.compile(rf"\b{re.escape(long_fn)}\(a\)")
    raw_pat = re.compile(r"a\.b\[--a\.a\]")
    super_call = re.compile(r"^([A-Za-z_$][A-Za-z0-9_$]*)\(a\s*,\s*b\)$")

    fields: list[str] = []
    for stmt in stmts:
        if stmt.startswith(("var ", "JSv[", "KSv=")):
            continue
        # Superclass call: NAME(a, b) where NAME is itself a deserializer.
        sm = super_call.match(stmt)
        if sm:
            super_name = sm.group(1)
            if super_name in chain_cache:
                fields.extend(chain_cache[super_name])
            else:
                super_body = extract_fn_body(all_js, super_name)
                if super_body is not None:
                    super_fields = parse_deserializer_body(
                        super_body, primitives, all_js, chain_cache
                    )
                    chain_cache[super_name] = super_fields
                    fields.extend(super_fields)
            continue
        # Otherwise classify by the read primitive used.
        if cast_obj.search(stmt) or bare_obj.search(stmt):
            fields.append("OBJECT")
        elif str_pat.search(stmt):
            fields.append("STRING")
        elif bool_pat.search(stmt):
            fields.append("BOOLEAN")
        elif dbl_pat.search(stmt):
            fields.append("DOUBLE")
        elif long_pat.search(stmt):
            fields.append("LONG")
        elif raw_pat.search(stmt):
            fields.append("RAW")
    return fields


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--permutation", required=True)
    p.add_argument("--base-url", default=DEFAULT_BASE)
    p.add_argument(
        "--fragments",
        type=int,
        nargs="*",
        default=list(DEFAULT_FRAGMENTS),
        help="Deferred-fragment numbers loaded by the live app",
    )
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    main_url = f"{args.base_url}/{args.permutation}.cache.js"
    print(f"→ downloading main bundle: {main_url}", file=sys.stderr)
    main_js = fetch(main_url)

    fragments: list[str] = []
    for n in args.fragments:
        url = f"{args.base_url}/deferredjs/{args.permutation}/{n}.cache.js"
        print(f"→ downloading fragment {n}: {url}", file=sys.stderr)
        raw = fetch(url)
        decoded = decode_fragment(raw)
        if decoded is None:
            print(f"  ⚠ fragment {n} has no recognized wrapper; skipping", file=sys.stderr)
            continue
        fragments.append(decoded)

    all_js = "\n".join([main_js, *fragments])
    print(f"  bundle: {len(all_js):,} chars", file=sys.stderr)

    print("→ discovering read primitives…", file=sys.stderr)
    primitives = discover_primitive_names(all_js)
    print(f"  primitives: {primitives}", file=sys.stderr)

    print("→ collecting Lose It! class constants…", file=sys.stderr)
    constants = collect_class_constants(all_js)
    print(f"  constants: {len(constants)}", file=sys.stderr)

    print("→ collecting serializer-table entries…", file=sys.stderr)
    table = collect_serializer_table(all_js)
    print(f"  entries: {len(table)}", file=sys.stderr)

    # Detect enums by their instantiate-function body: it pops a token
    # for the ordinal (``b=a.b[--a.a]``) and returns ``array[b]``. Regular
    # classes' instantiate functions just do ``new wjh; return``.
    enum_pat = re.compile(
        r"=\s*a\.b\[--a\.a\]\s*;[\s\S]*return\s+[A-Za-z_][\w$]*\s*\[\s*[a-z]\s*\]"
    )

    schemas: dict[str, dict] = {}
    chain_cache: dict[str, list[str]] = {}
    skipped = 0
    for var, fqcn in constants.items():
        entry = table.get(var)
        if entry is None:
            skipped += 1
            continue
        instantiate, deserialize, _serialize = entry
        body = extract_fn_body(all_js, deserialize)
        if body is None:
            skipped += 1
            continue
        fields = parse_deserializer_body(body, primitives, all_js, chain_cache)

        is_enum = False
        inst_body = extract_fn_body(all_js, instantiate)
        if inst_body is not None and enum_pat.search(inst_body):
            is_enum = True

        schemas[fqcn] = {
            "deserialize_fn": deserialize,
            "is_enum": is_enum,
            "fields": fields,
        }

    print(
        f"→ extracted {len(schemas)} schemas (skipped {skipped} types without table entries)",
        file=sys.stderr,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "permutation": args.permutation,
        "primitives": primitives,
        "schemas": schemas,
    }
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"✓ wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
