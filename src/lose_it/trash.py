"""Trash sinks for :meth:`LoseIt.delete_entry`.

The Lose It! API has no soft-delete or undo. To make every delete
recoverable, both the CLI and SDK route deletes through a **trash
sink** that captures the full entry payload **before** the wire
delete fires.

Public surface (per ``docs/backup-spec.md`` §9):

- :class:`TrashReceipt`   — return value of ``stash``: ``where`` (human
  pointer), ``payload`` (the entry dict, always present), ``stashed_at``.
- :class:`TrashSink`      — protocol; ``stash(entry) -> TrashReceipt``.
  Implementations MUST be synchronous and MUST raise on failure (a
  silently-dropping sink would defeat the recovery contract).
- :class:`DeleteResult`   — what ``LoseIt.delete_entry`` returns.
- :class:`DeleteSafetyError` — raised when the caller opts out of a sink
  without acknowledging the loss of recoverability.
- :class:`LocalFileTrashSink` — default; appends JSONL to
  ``~/.local/share/loseit/trash.jsonl`` with ``chmod 600``.
- :class:`ConsoleTrashSink`   — echoes TOON/JSON to stdout or stderr.
- :class:`ChainedTrashSink`   — fan-out to N sinks; all must succeed.

The CLI's ``loseit delete`` uses :class:`LocalFileTrashSink` by default
and ``loseit restore-trash`` replays the records.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, runtime_checkable

import toon_format

from .models import FoodLogEntry


def default_trash_file() -> Path:
    """Resolve the default trash file path at call time.

    Late binding (re-expanding ``~`` on every call) lets tests override
    ``HOME`` via ``monkeypatch.setenv`` without having to patch a module-
    level constant.
    """
    return Path("~/.local/share/loseit/trash.jsonl").expanduser()


# Module-level constant kept for callers that import the name; the
# function above is the source of truth.
DEFAULT_TRASH_FILE = default_trash_file()


@dataclass(frozen=True)
class TrashReceipt:
    """Returned by a :class:`TrashSink` to identify *where* the entry was stashed.

    Fields are designed for echoing in conversation logs and audit trails:

    - ``where``      — human-readable location (e.g. ``"trash.jsonl#42"``,
      ``"db:trash/abc"``, ``"stderr"``).
    - ``payload``    — the JSON-safe entry dict; always present so the
      caller has the data inline in case ``where`` later becomes
      inaccessible (container died, db wiped, ...).
    - ``stashed_at`` — UTC ISO 8601 ``+00:00`` of when the sink committed.
    """

    where: str
    payload: dict[str, Any]
    stashed_at: str


@runtime_checkable
class TrashSink(Protocol):
    """A sink for deleted entries.

    Implementations MUST be synchronous and MUST raise on failure — a
    sink that silently drops records would defeat the recovery contract.
    """

    def stash(self, entry: FoodLogEntry) -> TrashReceipt: ...


@dataclass(frozen=True)
class DeleteResult:
    """Return value of :meth:`LoseIt.delete_entry`.

    Carries the deleted entry's JSON projection, the receipts from every
    trash sink that absorbed it, and the UTC ISO timestamp of when the
    wire delete fired.
    """

    entry: dict[str, Any]
    trash_receipts: list[TrashReceipt] = field(default_factory=list)
    deleted_at: str = ""


class DeleteSafetyError(RuntimeError):
    """Raised when :meth:`LoseIt.delete_entry` would proceed without a sink.

    Use ``acknowledge_no_trash=True`` to explicitly opt out (and discard
    any chance of recovering the entry).
    """


def _utc_now_iso() -> str:
    """UTC ISO 8601 ``+00:00`` to second precision — matches the trash schema."""
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _entry_user_name(entry: FoodLogEntry) -> str:
    """Best-effort user identifier to embed in the trash record.

    The FoodLogEntry today doesn't carry the account it came from, so
    this is a placeholder hook — the CLI populates it via the
    ``user_name`` kwarg on :class:`LocalFileTrashSink`. Stays a plain
    string ("") if neither path supplies one, so the JSON shape is
    stable.
    """
    return ""


class LocalFileTrashSink:
    """Append one JSONL line per stash. Creates the file with mode ``0o600``.

    Each line is a JSON object: ``{"stashed_at", "user_name", "entry"}``.
    The ``entry`` block is exactly :meth:`FoodLogEntry.to_dict`.

    ``user_name`` is an optional string to embed in the record (defaults
    to ``""``); the CLI passes the resolved account email so trash files
    are auditable when multiple LoseIt accounts share the same machine.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        user_name: str = "",
    ) -> None:
        self.path = path if path is not None else default_trash_file()
        self.user_name = user_name

    def stash(self, entry: FoodLogEntry) -> TrashReceipt:
        """Append one JSONL line; fsync; chmod 600. Raises on I/O failure."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stashed_at = _utc_now_iso()
        payload = entry.to_dict()
        record = {
            "stashed_at": stashed_at,
            "user_name": self.user_name or _entry_user_name(entry),
            "entry": payload,
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        # Set the mode on first creation (and re-set it idempotently).
        os.chmod(self.path, 0o600)
        # Line count is the 1-based ordinal of the line we just wrote.
        n = sum(1 for _ in self.path.read_text(encoding="utf-8").splitlines())
        return TrashReceipt(
            where=f"{self.path}#{n}",
            payload=payload,
            stashed_at=stashed_at,
        )


class ConsoleTrashSink:
    """Echo the trash record to stdout or stderr in TOON or JSON.

    Useful for agent-framework callers that want the record to land in
    the conversation log rather than (or in addition to) a local file.
    The ``where`` returned is ``"stdout"`` or ``"stderr"`` — the receipt
    is informational; the durable artifact is the conversation transcript
    the framework captures.
    """

    def __init__(
        self,
        *,
        stream: str = "stderr",
        format: str = "toon",
        sink_stream: TextIO | None = None,
    ) -> None:
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"stream must be 'stdout' or 'stderr', got {stream!r}")
        if format not in ("toon", "json"):
            raise ValueError(f"format must be 'toon' or 'json', got {format!r}")
        self.stream_name = stream
        self.format = format
        # ``sink_stream`` is a test hook so callers can inject an in-memory
        # stream; in production we resolve from ``sys`` at write time.
        self._sink_stream = sink_stream

    def _resolve_stream(self) -> TextIO:
        if self._sink_stream is not None:
            return self._sink_stream
        return sys.stderr if self.stream_name == "stderr" else sys.stdout

    def stash(self, entry: FoodLogEntry) -> TrashReceipt:
        payload = entry.to_dict()
        stashed_at = _utc_now_iso()
        record = {
            "stashed_at": stashed_at,
            "user_name": _entry_user_name(entry),
            "entry": payload,
        }
        out = self._resolve_stream()
        if self.format == "toon":
            text = toon_format.encode(record)
        else:
            text = json.dumps(record, separators=(",", ":"), sort_keys=False)
        out.write(text)
        if not text.endswith("\n"):
            out.write("\n")
        out.flush()
        return TrashReceipt(
            where=self.stream_name,
            payload=payload,
            stashed_at=stashed_at,
        )


class ChainedTrashSink:
    """Fan the call out to N inner sinks; **all** must succeed.

    If the third sink raises, the first two have already written — and
    appending is the only operation, so there's nothing to roll back
    (per spec §9.7 question 3). The exception propagates so the caller
    can abort the wire delete; the receipt from the last successful sink
    is implicitly captured by the calling :meth:`delete_entry` only when
    the chain succeeds end-to-end.
    """

    def __init__(self, sinks: list[TrashSink]) -> None:
        if not sinks:
            raise ValueError("ChainedTrashSink requires at least one inner sink")
        self.sinks = list(sinks)

    def stash(self, entry: FoodLogEntry) -> TrashReceipt:
        last: TrashReceipt | None = None
        for sink in self.sinks:
            # Any sink raising propagates immediately — no rollback (see
            # docstring + spec §9.7 q3).
            last = sink.stash(entry)
        # ``last`` is never ``None`` here because ``__init__`` rejects
        # an empty list, but assert for the type checker's sake.
        assert last is not None
        return last


__all__ = [
    "DEFAULT_TRASH_FILE",
    "ChainedTrashSink",
    "ConsoleTrashSink",
    "DeleteResult",
    "DeleteSafetyError",
    "LocalFileTrashSink",
    "TrashReceipt",
    "TrashSink",
    "default_trash_file",
]
