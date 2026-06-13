"""On-disk backup file format library.

This subpackage owns every serialization choice for the backup feature
described in :mod:`docs/backup-spec.md` §4. Higher-level orchestration
(fetch, discovery, restore) lives in sibling modules; this module is
the file-format contract — TOON schema dataclasses, atomic-write
primitive, and schema-version guards.

Anyone producing a grain file, foods cache, or index file goes through
the dataclasses + writers here. Anyone consuming them goes through the
readers here. That way the on-disk shape is the only contract between
backup writers and backup readers (per the impl-plan §1 principle:
"File format is the interface").
"""

from __future__ import annotations

from ._discovery import (
    DiscoveryProbe,
    DiscoveryResult,
    discover_earliest_day,
)
from ._fetch import (
    FetchStatus,
    Grain,
    fetch_grain,
    grain_entry_sort_key,
    to_grain_entry,
    update_food_cache,
)
from ._fs import (
    SCHEMA_VERSION,
    AccountRef,
    FoodCacheEntry,
    FoodsDoc,
    GrainBounds,
    GrainDoc,
    GrainEntry,
    IndexDoc,
    SchemaVersionMismatch,
    atomic_write_text,
    read_foods_file,
    read_grain_file,
    read_index_file,
    same_account,
    write_foods_file,
    write_grain_file,
    write_index_file,
)

__all__ = [
    "SCHEMA_VERSION",
    "AccountRef",
    "DiscoveryProbe",
    "DiscoveryResult",
    "FetchStatus",
    "FoodCacheEntry",
    "FoodsDoc",
    "Grain",
    "GrainBounds",
    "GrainDoc",
    "GrainEntry",
    "IndexDoc",
    "SchemaVersionMismatch",
    "atomic_write_text",
    "discover_earliest_day",
    "fetch_grain",
    "grain_entry_sort_key",
    "read_foods_file",
    "read_grain_file",
    "read_index_file",
    "same_account",
    "to_grain_entry",
    "update_food_cache",
    "write_foods_file",
    "write_grain_file",
    "write_index_file",
]
