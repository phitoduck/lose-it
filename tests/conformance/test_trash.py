"""Unit tests for :mod:`lose_it.trash` and the rewritten ``LoseIt.delete_entry``.

The whole point of the trash module is: every delete is recoverable.
These tests pin down the invariants:

- :class:`LocalFileTrashSink` appends one JSONL line, ``chmod 600``.
- :class:`ChainedTrashSink` propagates partial-success state — sinks that
  already wrote stay written when a later sink raises (spec §9.7 q3).
- :meth:`LoseIt.delete_entry` calls ``trash_sink.stash`` BEFORE the wire
  delete. If ``stash`` raises, the wire call never fires.
- ``trash_sink=None`` is refused unless ``acknowledge_no_trash=True``.

All tests run hermetically — no HTTP, no real filesystem outside
``tmp_path``, no real Lose It! account.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lose_it import LoseIt
from lose_it.core import entries as _entries
from lose_it.core._config import Config
from lose_it.models import FoodLogEntry
from lose_it.trash import (
    ChainedTrashSink,
    DeleteSafetyError,
    LocalFileTrashSink,
    TrashReceipt,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_food_log_entry(
    *,
    food_name: str = "Test Wrap",
    food_brand: str = "TestBrand",
    food_id_hex: str = "abc" * 8 + "abcd1234abcd1234",  # 32-hex would land in a different
    # pk shape; tests below only care that the entry round-trips via to_dict.
) -> FoodLogEntry:
    """Build a minimal :class:`FoodLogEntry` suitable for trash tests.

    The fields are the ones the dataclass requires; values are arbitrary
    but stable so JSON output is byte-stable across runs.
    """
    # FoodLogEntry's pk byte arrays are 16-int lists in *response form*
    # (a.k.a. byte-reversed). We don't need real bytes — any 16-int list
    # works for the JSON projection.
    pk = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    return FoodLogEntry(
        food_category="Test",
        food_name=food_name,
        food_brand=food_brand,
        food_pk_response=list(pk),
        entry_pk_response=list(reversed(pk)),
        entry_day_key="Z66oWlo",
        context_day_key="Z66oWlo",
        day_num=9294,
        hours_from_gmt=-6,
        meal_ordinal=3,  # snacks
        extra_ordinal=0,
        food_measure_ordinal=27,
        servings=1.0,
        food_identifier_code="DoP_mj",
        nutrients_ordered=[(0, 70.0), (9, 300.0)],
    )


@pytest.fixture
def test_config() -> Config:
    """Same shape as :func:`tests.conftest.test_config` but local to this module.

    The trash tests don't share fixtures with the conformance suite so
    pasting the Config inline keeps the file self-contained.
    """
    return Config(
        user_id="12345678",
        user_name="test.user",
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )


# ── LocalFileTrashSink ───────────────────────────────────────────────────────


def test_localfile_sink_appends_one_jsonl_line(tmp_path: Path) -> None:
    sink = LocalFileTrashSink(path=tmp_path / "trash.jsonl")
    entry = _make_food_log_entry(food_name="test")
    receipt = sink.stash(entry)
    assert receipt.where == f"{tmp_path / 'trash.jsonl'}#1"
    content = (tmp_path / "trash.jsonl").read_text()
    assert content.count("\n") == 1
    obj = json.loads(content.strip())
    assert obj["entry"]["food_name"] == "test"
    assert isinstance(obj["stashed_at"], str)
    # The receipt mirrors the payload (the on-disk JSON re-stringifies
    # int keys in ``nutrients``, so compare the food_id-tier scalars).
    assert receipt.payload["food_name"] == obj["entry"]["food_name"]
    assert receipt.payload["food_identifier_code"] == obj["entry"]["food_identifier_code"]


def test_localfile_sink_creates_missing_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "trash.jsonl"
    sink = LocalFileTrashSink(path=target)
    sink.stash(_make_food_log_entry())
    assert target.exists()
    assert target.read_text().count("\n") == 1


def test_localfile_sink_chmods_600(tmp_path: Path) -> None:
    sink = LocalFileTrashSink(path=tmp_path / "trash.jsonl")
    sink.stash(_make_food_log_entry())
    mode = oct((tmp_path / "trash.jsonl").stat().st_mode)[-3:]
    assert mode == "600"


def test_localfile_sink_appends_across_calls(tmp_path: Path) -> None:
    """A second stash adds a second line; the receipt#N increments."""
    sink = LocalFileTrashSink(path=tmp_path / "trash.jsonl")
    r1 = sink.stash(_make_food_log_entry(food_name="first"))
    r2 = sink.stash(_make_food_log_entry(food_name="second"))
    assert r1.where.endswith("#1")
    assert r2.where.endswith("#2")
    content = (tmp_path / "trash.jsonl").read_text()
    assert content.count("\n") == 2
    lines = content.strip().split("\n")
    assert json.loads(lines[0])["entry"]["food_name"] == "first"
    assert json.loads(lines[1])["entry"]["food_name"] == "second"


def test_localfile_sink_records_user_name(tmp_path: Path) -> None:
    sink = LocalFileTrashSink(path=tmp_path / "trash.jsonl", user_name="me@example.com")
    sink.stash(_make_food_log_entry())
    obj = json.loads((tmp_path / "trash.jsonl").read_text().strip())
    assert obj["user_name"] == "me@example.com"


# ── ChainedTrashSink ─────────────────────────────────────────────────────────


def test_chained_sink_all_must_succeed_no_rollback(tmp_path: Path) -> None:
    """If a later sink raises, earlier writes stay (spec §9.7 q3)."""
    path_a = tmp_path / "a.jsonl"
    sink_a = LocalFileTrashSink(path=path_a)

    class FailingSink:
        def stash(self, entry: FoodLogEntry) -> TrashReceipt:
            raise OSError("disk full")

    chain = ChainedTrashSink([sink_a, FailingSink()])
    with pytest.raises(OSError, match="disk full"):
        chain.stash(_make_food_log_entry())
    # sink_a's record stayed on disk — appending is the only operation.
    assert path_a.read_text().count("\n") == 1


def test_chained_sink_returns_last_receipt(tmp_path: Path) -> None:
    sink_a = LocalFileTrashSink(path=tmp_path / "a.jsonl")
    sink_b = LocalFileTrashSink(path=tmp_path / "b.jsonl")
    chain = ChainedTrashSink([sink_a, sink_b])
    receipt = chain.stash(_make_food_log_entry())
    # The receipt comes from the last sink (sink_b).
    assert receipt.where.startswith(str(tmp_path / "b.jsonl"))


def test_chained_sink_rejects_empty_list() -> None:
    with pytest.raises(ValueError):
        ChainedTrashSink([])


# ── LoseIt.delete_entry — the invariant ──────────────────────────────────────


def test_delete_entry_stashes_before_wire_call(
    monkeypatch: pytest.MonkeyPatch, test_config: Config
) -> None:
    """Stash succeeds, THEN the wire delete fires — in that order."""
    calls: list[str] = []
    monkeypatch.setattr(
        _entries,
        "delete",
        lambda http, entry: calls.append("wire_delete"),
    )

    class SpySink:
        def stash(self, entry: FoodLogEntry) -> TrashReceipt:
            calls.append("stash")
            return TrashReceipt(
                where="spy#1",
                payload=entry.to_dict(),
                stashed_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
            )

    li = LoseIt(test_config, token="x")
    result = li.delete_entry(_make_food_log_entry(), trash_sink=SpySink(), confirm=True)
    assert calls == ["stash", "wire_delete"]
    assert len(result.trash_receipts) == 1
    assert result.trash_receipts[0].where == "spy#1"
    assert result.deleted_at  # populated
    assert result.entry["food_name"] == "Test Wrap"


def test_delete_entry_aborts_on_stash_failure(
    monkeypatch: pytest.MonkeyPatch, test_config: Config
) -> None:
    """If stash raises, the wire delete is NEVER called."""
    calls: list[str] = []
    monkeypatch.setattr(
        _entries,
        "delete",
        lambda http, entry: calls.append("wire_delete"),
    )

    class FailSink:
        def stash(self, entry: FoodLogEntry) -> TrashReceipt:
            raise OSError("oops")

    li = LoseIt(test_config, token="x")
    with pytest.raises(OSError, match="oops"):
        li.delete_entry(_make_food_log_entry(), trash_sink=FailSink(), confirm=True)
    assert "wire_delete" not in calls


def test_delete_entry_with_none_sink_refuses_without_ack(test_config: Config) -> None:
    li = LoseIt(test_config, token="x")
    with pytest.raises(DeleteSafetyError, match="trash_required"):
        li.delete_entry(_make_food_log_entry(), trash_sink=None, confirm=True)


def test_delete_entry_with_none_sink_and_ack_is_allowed(
    monkeypatch: pytest.MonkeyPatch, test_config: Config
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_entries, "delete", lambda http, entry: calls.append("wire_delete"))
    li = LoseIt(test_config, token="x")
    result = li.delete_entry(
        _make_food_log_entry(),
        trash_sink=None,
        acknowledge_no_trash=True,
        confirm=True,
    )
    assert calls == ["wire_delete"]
    assert result.trash_receipts == []


def test_delete_entry_default_sink_writes_local_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    test_config: Config,
) -> None:
    """Default sink = LocalFileTrashSink expanded via $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(_entries, "delete", lambda http, entry: calls.append("delete"))
    li = LoseIt(test_config, token="x")
    result = li.delete_entry(_make_food_log_entry())
    assert calls == ["delete"]
    assert len(result.trash_receipts) == 1
    trash_path = tmp_path / ".config" / "loseit" / "trash.jsonl"
    assert trash_path.exists()
    assert trash_path.read_text().count("\n") == 1


# ── LoseIt.restore_trash ─────────────────────────────────────────────────────


def test_restore_trash_dry_run_does_not_touch_file_or_call_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    test_config: Config,
) -> None:
    """--dry-run returns a plan without log_food or file mutation."""
    trash = tmp_path / "trash.jsonl"
    rec = {
        "stashed_at": "2026-06-12T20:00:00+00:00",
        "user_name": "me",
        "entry": {
            "food_id": "a" * 32,
            "food_name": "Stub",
            "meal": "snacks",
            "date": "2026-06-12",
            "servings": 1.5,
        },
    }
    trash.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    li = LoseIt(test_config, token="x")
    monkeypatch.setattr(
        li,
        "log_food",
        lambda *a, **kw: pytest.fail("log_food should not be called on dry_run"),
    )
    result = li.restore_trash(trash_file=trash, dry_run=True)
    assert result["dry_run"] is True
    assert result["consumed"] is False
    assert result["food_id"] == "a" * 32
    assert result["servings"] == 1.5
    # File untouched.
    assert trash.read_text() == json.dumps(rec) + "\n"
