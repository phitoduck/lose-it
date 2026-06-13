"""TOON file format library for the backup feature.

This module is intentionally narrow: dataclasses for the on-disk
shapes (§4 of `docs/backup-spec.md`), a single atomic-write helper
(``tmp -> fsync -> os.replace``), readers + writers that go through
:mod:`toon_format`, and a schema-version guard.

Design choices worth flagging for readers:

* **Top-level key order is fixed.** The spec spells it out in §4.1-§4.3
  and the BDDs assert against it. Writers always serialize through a
  dict literal in that exact order so the resulting TOON document is
  human-grep-able and stable across runs.
* **Entries are sorted on write, never re-sorted on read.** Spec §4.1
  orders rows by ``(day_num asc, meal_ordinal asc, created_at asc)``
  so diffs between two snapshots are stable. The reader trusts the
  file's ordering — it is the truth.
* **Dates are serialized as ISO ``YYYY-MM-DD`` strings.** TOON only
  accepts JSON-serializable values; the wire form for a date in this
  archive is the ISO string. Readers parse them back into
  :class:`datetime.date`.
* **The schema-version check is the only guard.** Anything else that
  doesn't decode is a parse-or-raise error from :mod:`toon_format`
  or from dataclass construction itself.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import toon_format
from loguru import logger

SCHEMA_VERSION = 1


class SchemaVersionMismatch(ValueError):
    """Raised when a file's ``schema_version`` is newer than this build understands.

    Per spec §4 (case "Schema bump"), the writer refuses to overwrite
    higher-version files and the reader refuses to load them. This
    exception is what the reader raises.
    """


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountRef:
    """The ``account:`` block pinned into every backup file.

    Spec §8 ("User switches accounts mid-archive"): every file carries
    ``user_id`` and ``user_name`` so a later restore can refuse to run
    against the wrong account (the ``--strict-account`` guard).
    """

    user_id: str
    user_name: str


@dataclass(frozen=True)
class GrainBounds:
    """The ``grain:`` block for a grain file."""

    kind: str  # "day" | "week" | "month"
    start: date
    end: date


@dataclass(frozen=True)
class GrainEntry:
    """One diary entry as recorded in a grain file. See spec §4.1.

    Fields preserve everything we need for human review AND for re-logging
    via :meth:`lose_it.LoseIt.log_food`. The minimum re-log set is
    ``(food_id, meal_ordinal, servings, date)``; the rest is for human
    inspection and the upsert key ``(food_id, created_at)`` used by
    safe-mode restore.
    """

    date: date
    day_num: int
    meal: str
    meal_ordinal: int
    food_id: str
    food_name: str
    food_brand: str
    food_category: str
    food_identifier_code: str
    food_measure_ordinal: int
    food_measure_unit: str
    servings: float
    calories: float | None
    nutrients: dict[str, float] = field(default_factory=dict)
    nutrients_by_label: dict[str, float] = field(default_factory=dict)
    entry_pk_response: list[int] = field(default_factory=list)
    food_pk_response: list[int] = field(default_factory=list)
    entry_day_key: str = ""
    context_day_key: str = ""
    hours_from_gmt: int = 0
    created_at: str = ""
    modified_at: str = ""
    ingest_ts: str = ""


@dataclass(frozen=True)
class GrainDoc:
    """A complete grain file (§4.1)."""

    account: AccountRef
    grain: GrainBounds
    generated_at: str
    entries: list[GrainEntry] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class FoodCacheEntry:
    """One row in ``foods.toon`` (§4.2)."""

    food_id: str
    last_described_at: str
    first_seen_date: date
    last_seen_date: date
    name: str
    brand: str
    category: str
    primary_serving: dict[str, Any]
    cross_class_conversion: dict[str, Any]
    nutrients_per_serving: dict[str, float]
    raw_nutrients_by_ord: dict[str, float]


@dataclass(frozen=True)
class FoodsDoc:
    """The top-level ``foods.toon`` file (§4.2)."""

    account: AccountRef
    foods: dict[str, FoodCacheEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class IndexDoc:
    """The top-level ``index.toon`` file (§4.3)."""

    account: AccountRef
    grain: str  # "day" | "week" | "month"
    discovered_earliest_day: date | None
    discovered_at: str
    schema_version: int = SCHEMA_VERSION


# ── Helpers ──────────────────────────────────────────────────────────────────


def same_account(a: AccountRef, b: AccountRef) -> bool:
    """Strict-account guard helper.

    Returns ``True`` iff both ``user_id`` and ``user_name`` match. The
    backup orchestrator (T6) uses this when deciding whether the file on
    disk belongs to the currently-logged-in account.
    """
    return a == b


# ── Atomic write ─────────────────────────────────────────────────────────────


def atomic_write_text(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` atomically.

    Protocol (per spec §6.4 / impl-plan §4 row "Atomic-write tmp → fsync
    → os.replace"):

    1. Ensure parent directory exists.
    2. Write the body to a sibling ``.tmp.<rand>`` file.
    3. ``fsync`` the temp file so the bytes hit the disk.
    4. ``os.replace`` the temp file into place — atomic on POSIX.

    Postcondition: no file matching ``{path}.tmp*`` remains under the
    target directory after a successful call. A failure mid-way leaves
    the original target untouched (the rename never happened).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # The tmp suffix carries enough entropy to survive concurrent
    # writers to the same target. A bare ``.tmp`` would race if two
    # backups ran against the same root (a user can do this — see
    # spec §8 "Backup ported between machines").
    tmp = path.with_suffix(path.suffix + f".tmp.{secrets.token_hex(4)}")
    tmp.write_text(body, encoding="utf-8")
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ── Encoding / decoding ──────────────────────────────────────────────────────


def _entry_to_dict(e: GrainEntry) -> dict[str, Any]:
    """Project a :class:`GrainEntry` into a JSON-serializable dict.

    The field order here matches spec §4.1's example row exactly so the
    TOON encoder emits the keys in the documented order.
    """
    return {
        "date": e.date.isoformat(),
        "day_num": e.day_num,
        "meal": e.meal,
        "meal_ordinal": e.meal_ordinal,
        "food_id": e.food_id,
        "food_name": e.food_name,
        "food_brand": e.food_brand,
        "food_category": e.food_category,
        "food_identifier_code": e.food_identifier_code,
        "food_measure_ordinal": e.food_measure_ordinal,
        "food_measure_unit": e.food_measure_unit,
        "servings": e.servings,
        "calories": e.calories,
        "nutrients": dict(e.nutrients),
        "nutrients_by_label": dict(e.nutrients_by_label),
        "entry_pk_response": list(e.entry_pk_response),
        "food_pk_response": list(e.food_pk_response),
        "entry_day_key": e.entry_day_key,
        "context_day_key": e.context_day_key,
        "hours_from_gmt": e.hours_from_gmt,
        "created_at": e.created_at,
        "modified_at": e.modified_at,
        "ingest_ts": e.ingest_ts,
    }


def _entry_from_dict(d: dict[str, Any]) -> GrainEntry:
    """Inverse of :func:`_entry_to_dict`."""
    return GrainEntry(
        date=date.fromisoformat(d["date"]),
        day_num=int(d["day_num"]),
        meal=str(d["meal"]),
        meal_ordinal=int(d["meal_ordinal"]),
        food_id=str(d["food_id"]),
        food_name=str(d["food_name"]),
        food_brand=str(d["food_brand"]),
        food_category=str(d["food_category"]),
        food_identifier_code=str(d["food_identifier_code"]),
        food_measure_ordinal=int(d["food_measure_ordinal"]),
        food_measure_unit=str(d["food_measure_unit"]),
        servings=float(d["servings"]),
        calories=None if d.get("calories") is None else float(d["calories"]),
        nutrients={str(k): float(v) for k, v in (d.get("nutrients") or {}).items()},
        nutrients_by_label={
            str(k): float(v) for k, v in (d.get("nutrients_by_label") or {}).items()
        },
        entry_pk_response=[int(x) for x in (d.get("entry_pk_response") or [])],
        food_pk_response=[int(x) for x in (d.get("food_pk_response") or [])],
        entry_day_key=str(d.get("entry_day_key", "")),
        context_day_key=str(d.get("context_day_key", "")),
        hours_from_gmt=int(d.get("hours_from_gmt", 0)),
        created_at=str(d.get("created_at", "")),
        modified_at=str(d.get("modified_at", "")),
        ingest_ts=str(d.get("ingest_ts", "")),
    )


def _account_to_dict(a: AccountRef) -> dict[str, Any]:
    return {"user_id": a.user_id, "user_name": a.user_name}


def _account_from_dict(d: dict[str, Any]) -> AccountRef:
    return AccountRef(user_id=str(d["user_id"]), user_name=str(d["user_name"]))


def _sort_entries(entries: list[GrainEntry]) -> list[GrainEntry]:
    """Canonical sort order from spec §4.1.

    Ordered ``(day_num asc, meal_ordinal asc, created_at asc)`` — the
    same three-key sort used for stable diffs between two snapshots.
    """
    return sorted(entries, key=lambda e: (e.day_num, e.meal_ordinal, e.created_at))


def _check_schema_version(payload: dict[str, Any], path: Path) -> None:
    """Raise :class:`SchemaVersionMismatch` if the file is newer than us.

    A future schema bump bumps :data:`SCHEMA_VERSION`; the migration
    tooling for that bump is out-of-scope for v1 (spec §12).
    """
    sv = payload.get("schema_version")
    if not isinstance(sv, int):
        raise ValueError(f"missing or non-integer schema_version in {path}: {sv!r}")
    if sv > SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"schema version {sv} not supported (this build understands {SCHEMA_VERSION})"
        )


# ── Grain file IO ────────────────────────────────────────────────────────────


def write_grain_file(path: Path, doc: GrainDoc) -> None:
    """Write a grain file (§4.1).

    Top-level keys are emitted in fixed order: ``schema_version``,
    ``account``, ``grain``, ``generated_at``, ``entries``. Entries are
    sorted by ``(day_num, meal_ordinal, created_at)`` before encoding.
    """
    if doc.schema_version > SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"refusing to write schema_version={doc.schema_version} "
            f"(this build understands {SCHEMA_VERSION})"
        )
    sorted_entries = _sort_entries(list(doc.entries))
    payload: dict[str, Any] = {
        "schema_version": doc.schema_version,
        "account": _account_to_dict(doc.account),
        "grain": {
            "kind": doc.grain.kind,
            "start": doc.grain.start.isoformat(),
            "end": doc.grain.end.isoformat(),
        },
        "generated_at": doc.generated_at,
        "entries": [_entry_to_dict(e) for e in sorted_entries],
    }
    body = toon_format.encode(payload)
    if not body.endswith("\n"):
        body += "\n"
    atomic_write_text(path, body)
    logger.debug("wrote grain file path={} entries={}", path, len(sorted_entries))


def read_grain_file(path: Path) -> GrainDoc:
    """Parse a grain file. Raises :class:`SchemaVersionMismatch` on a newer file."""
    text = path.read_text(encoding="utf-8")
    payload = toon_format.decode(text)
    if not isinstance(payload, dict):
        raise ValueError(f"grain file {path} did not decode to a mapping")
    _check_schema_version(payload, path)
    account = _account_from_dict(payload["account"])
    grain_block = payload["grain"]
    grain = GrainBounds(
        kind=str(grain_block["kind"]),
        start=date.fromisoformat(grain_block["start"]),
        end=date.fromisoformat(grain_block["end"]),
    )
    raw_entries = payload.get("entries") or []
    entries = [_entry_from_dict(e) for e in raw_entries]
    return GrainDoc(
        account=account,
        grain=grain,
        generated_at=str(payload["generated_at"]),
        entries=entries,
        schema_version=int(payload["schema_version"]),
    )


# ── Foods file IO ────────────────────────────────────────────────────────────


def _food_to_dict(f: FoodCacheEntry) -> dict[str, Any]:
    return {
        "food_id": f.food_id,
        "last_described_at": f.last_described_at,
        "first_seen_date": f.first_seen_date.isoformat(),
        "last_seen_date": f.last_seen_date.isoformat(),
        "name": f.name,
        "brand": f.brand,
        "category": f.category,
        "primary_serving": dict(f.primary_serving),
        "cross_class_conversion": dict(f.cross_class_conversion),
        "nutrients_per_serving": dict(f.nutrients_per_serving),
        "raw_nutrients_by_ord": dict(f.raw_nutrients_by_ord),
    }


def _food_from_dict(d: dict[str, Any]) -> FoodCacheEntry:
    return FoodCacheEntry(
        food_id=str(d["food_id"]),
        last_described_at=str(d["last_described_at"]),
        first_seen_date=date.fromisoformat(d["first_seen_date"]),
        last_seen_date=date.fromisoformat(d["last_seen_date"]),
        name=str(d["name"]),
        brand=str(d["brand"]),
        category=str(d["category"]),
        primary_serving=dict(d.get("primary_serving") or {}),
        cross_class_conversion=dict(d.get("cross_class_conversion") or {}),
        nutrients_per_serving={
            str(k): float(v) for k, v in (d.get("nutrients_per_serving") or {}).items()
        },
        raw_nutrients_by_ord={
            str(k): float(v) for k, v in (d.get("raw_nutrients_by_ord") or {}).items()
        },
    )


def write_foods_file(path: Path, doc: FoodsDoc) -> None:
    """Write ``foods.toon`` (§4.2)."""
    if doc.schema_version > SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"refusing to write schema_version={doc.schema_version} "
            f"(this build understands {SCHEMA_VERSION})"
        )
    payload: dict[str, Any] = {
        "schema_version": doc.schema_version,
        "account": _account_to_dict(doc.account),
        "foods": {fid: _food_to_dict(f) for fid, f in doc.foods.items()},
    }
    body = toon_format.encode(payload)
    if not body.endswith("\n"):
        body += "\n"
    atomic_write_text(path, body)
    logger.debug("wrote foods cache path={} foods={}", path, len(doc.foods))


def read_foods_file(path: Path) -> FoodsDoc:
    """Parse ``foods.toon``. Raises :class:`SchemaVersionMismatch` on a newer file."""
    text = path.read_text(encoding="utf-8")
    payload = toon_format.decode(text)
    if not isinstance(payload, dict):
        raise ValueError(f"foods file {path} did not decode to a mapping")
    _check_schema_version(payload, path)
    account = _account_from_dict(payload["account"])
    raw_foods = payload.get("foods") or {}
    foods = {str(fid): _food_from_dict(d) for fid, d in raw_foods.items()}
    return FoodsDoc(
        account=account,
        foods=foods,
        schema_version=int(payload["schema_version"]),
    )


# ── Index file IO ────────────────────────────────────────────────────────────


def write_index_file(path: Path, doc: IndexDoc) -> None:
    """Write ``index.toon`` (§4.3)."""
    if doc.schema_version > SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"refusing to write schema_version={doc.schema_version} "
            f"(this build understands {SCHEMA_VERSION})"
        )
    payload: dict[str, Any] = {
        "schema_version": doc.schema_version,
        "account": _account_to_dict(doc.account),
        "grain": doc.grain,
        "discovered_earliest_day": (
            doc.discovered_earliest_day.isoformat()
            if doc.discovered_earliest_day is not None
            else None
        ),
        "discovered_at": doc.discovered_at,
    }
    body = toon_format.encode(payload)
    if not body.endswith("\n"):
        body += "\n"
    atomic_write_text(path, body)
    logger.debug("wrote index path={} grain={}", path, doc.grain)


def read_index_file(path: Path) -> IndexDoc:
    """Parse ``index.toon``. Raises :class:`SchemaVersionMismatch` on a newer file."""
    text = path.read_text(encoding="utf-8")
    payload = toon_format.decode(text)
    if not isinstance(payload, dict):
        raise ValueError(f"index file {path} did not decode to a mapping")
    _check_schema_version(payload, path)
    account = _account_from_dict(payload["account"])
    discovered = payload.get("discovered_earliest_day")
    earliest = date.fromisoformat(discovered) if discovered else None
    return IndexDoc(
        account=account,
        grain=str(payload["grain"]),
        discovered_earliest_day=earliest,
        discovered_at=str(payload["discovered_at"]),
        schema_version=int(payload["schema_version"]),
    )
