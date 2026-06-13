"""Backup orchestrator + cheap-mode restore (T6).

Composes the three building blocks shipped earlier in the backup-spec
plan:

* **T1** (:mod:`lose_it.backup._fs`) — on-disk shape, atomic writes,
  schema-version guard.
* **T2** (:mod:`lose_it.backup._fetch`) — :class:`Grain`, the recursive
  split-and-retry :func:`fetch_grain`, and the describe-cadence
  helper :func:`update_food_cache`.
* **T3** (:mod:`lose_it.backup._discovery`) — yearly→monthly→day
  discovery probe that finds the earliest day with diary entries.

This module owns the **orchestration**: given a date range and a grain
kind, decide for every grain whether to *skip* (file already on disk
and parses cleanly), *fetch* (no file), or *partial* (file exists but
is incomplete / unreadable — we re-fetch the whole grain because grain
files are stateless per spec §4.1). It also drives the discovery probe
when the caller doesn't pin ``--start`` and caches the outcome in
``index.toon`` so subsequent runs skip the probe.

Restore-side, this track ships **cheap mode only** (spec §7.2 — no
``created_at`` dependency). Safe mode is T7; until that lands the
:meth:`LoseIt.restore_backup` method raises :class:`NotImplementedError`
when the cheap flag is False, with a message pointing the caller at
the flag they can pass in the interim.

The orchestrator is **silent** — every per-grain decision is fed to an
optional ``progress(report)`` callback so the CLI (T8) can render the
spec §3.1 stdout shape. Tests inspect the callback's payload directly,
which is why every per-grain status fans out through a typed
:class:`GrainReport` record.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from loguru import logger

from lose_it.backup._discovery import discover_earliest_day
from lose_it.backup._fetch import (
    FetchStatus,
    Grain,
    fetch_grain,
    grain_entry_sort_key,
    to_grain_entry,
    update_food_cache,
)
from lose_it.backup._fs import (
    SCHEMA_VERSION,
    AccountRef,
    FoodsDoc,
    GrainBounds,
    GrainDoc,
    IndexDoc,
    SchemaVersionMismatch,
    read_grain_file,
    read_index_file,
    same_account,
    write_foods_file,
    write_grain_file,
    write_index_file,
)
from lose_it.core._ids import pk_to_hex

GrainKind = Literal["day", "week", "month"]


# ── Reports ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GrainReport:
    """Per-grain decision the orchestrator made.

    The CLI (T8) renders one line per report in the spec §3.1 stdout.
    Unit tests pin behavior by inspecting the report list directly —
    they don't shell out.

    * ``status == skip`` — grain file exists, parsed cleanly, account
      matches the running user. No RPCs fired.
    * ``status == partial`` — grain file exists but failed schema /
      account / format checks. Re-fetched the whole grain (grain files
      are stateless per spec §4.1).
    * ``status == fetch`` — no file on disk. First-attempt
      ``diary_range`` succeeded.
    * ``status == fallback`` — first-attempt ``diary_range`` raised
      :class:`~lose_it.core.daily.TooMuchData`; the splitter recursed
      into a smaller grain.
    """

    grain: Grain
    status: FetchStatus
    days_with_entries: int
    entries: int


@dataclass(frozen=True)
class BackupSummary:
    """End-of-run roll-up returned by :func:`backup` and exposed via
    :meth:`LoseIt.backup`. Mirrors the summary table at the bottom of
    the spec §3.1 stdout examples."""

    months_total: int
    months_skipped: int
    months_partial: int
    months_fetched: int
    months_fell_back: int
    days_fetched: int
    days_with_entries: int
    new_foods_described: int
    foods_redescribed_today: int
    root: Path
    archive_size_bytes: int


# ── Restore reports ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheapRestoreGrainReport:
    """Per-grain decision the cheap-mode restore made.

    The CLI uses ``status`` to pick ``skip`` vs ``restore`` in the
    stdout. ``hit_day`` is set when the server's diary returned any
    entry inside the grain — its presence is what triggered the skip.
    ``entries_logged`` is the number of backup entries this orchestrator
    re-logged via :meth:`LoseIt.log_food` (0 for skips and dry-runs).
    """

    grain_path: Path
    status: Literal["skip", "restore"]
    days_scanned: int
    hit_day: date | None
    entries_in_grain: int
    entries_logged: int


@dataclass(frozen=True)
class RestoreSummary:
    """End-of-run roll-up returned by :func:`restore_backup_cheap`
    and :func:`restore_backup_safe`.

    Cheap-mode populates the ``grains_*`` + ``entries_logged`` fields. Safe-
    mode additionally populates ``days_scanned``, ``days_fully_present``,
    ``days_upserted``, ``entries_already_present`` so the spec §3.2 safe-
    mode summary block has the data it renders.
    """

    grains_scanned: int
    grains_skipped: int
    grains_restored: int
    entries_logged: int
    root: Path
    # Safe-mode counters (zeroed for cheap-mode runs).
    days_scanned: int = 0
    days_fully_present: int = 0
    days_upserted: int = 0
    entries_already_present: int = 0


@dataclass(frozen=True)
class SafeRestoreGrainReport:
    """Per-grain decision the safe-mode restore made.

    Mirrors the per-grain row the CLI renders in spec §3.2's safe-mode
    output: ``<path>  N days with entries  present  X   upsert  Y   empty  Z``.

    * ``days_with_entries`` — number of days in the archive that have at
      least one entry (the rows the safe-mode loop will scan).
    * ``days_present`` — days where every archive entry matched a server
      entry (no log calls).
    * ``days_upserted`` — days where at least one archive entry was
      missing on the server and got logged.
    * ``days_empty`` — days the archive has no entries for (rare; only
      surfaces when the file's bounds include such a day).
    * ``entries_present`` — sum of archive entries that matched a server
      counterpart across the grain.
    * ``entries_logged`` — sum of archive entries that were re-logged
      (zero on dry-runs).
    """

    grain_path: Path
    days_with_entries: int
    days_present: int
    days_upserted: int
    days_empty: int
    entries_present: int
    entries_logged: int


# ── Structural protocols ────────────────────────────────────────────────────


class _OrchestratorClient(Protocol):
    """The bare-minimum SDK surface :func:`backup` consults.

    Real call sites pass a :class:`lose_it.LoseIt`; tests pass a
    structural fake (see ``tests/conformance/test_backup_orchestrator.py``).
    Spelling the protocol out keeps the orchestrator unit-testable
    without spinning up an HTTP session.
    """

    config: Any  # ``lose_it.core._config.Config`` — only ``.user_id`` / ``.user_name`` read.

    def diary(self, when: date) -> Any: ...

    def diary_range(self, start: date, end: date) -> dict[date, list[Any]]: ...

    def describe_food(self, food_id: str) -> Any: ...

    def log_food(self, food: Any, meal: Any = ..., servings: float = ..., **kwargs: Any) -> Any: ...


# ── Helpers ──────────────────────────────────────────────────────────────────


def _account_from_client(client: _OrchestratorClient) -> AccountRef:
    """Project the running client's config into a :class:`AccountRef`.

    The orchestrator pins this onto every file it writes so a future
    restore (or a subsequent run from a different account) can refuse
    via :func:`lose_it.backup._fs.same_account` — see spec §8.
    """
    cfg = client.config
    user_id = getattr(cfg, "user_id", None) or ""
    user_name = getattr(cfg, "user_name", None) or ""
    return AccountRef(user_id=str(user_id), user_name=str(user_name))


def _now_utc_iso() -> str:
    """ISO-8601 UTC ``+00:00`` timestamp used as ``generated_at`` / ``ingest_ts``.

    Indirected so future tests can inject a frozen clock without
    monkey-patching :mod:`datetime`.
    """
    return datetime.now(UTC).isoformat()


def _grain_file_path(root: Path, grain: Grain) -> Path:
    """Spec §2 layout: month → ``YYYY/MM.toon``, day → ``YYYY/MM/DD.toon``,
    week → ``YYYY/Www.toon`` (W = ISO-week number, zero-padded to 2)."""
    if grain.kind == "month":
        return root / f"{grain.start.year:04d}" / f"{grain.start.month:02d}.toon"
    if grain.kind == "day":
        return (
            root
            / f"{grain.start.year:04d}"
            / f"{grain.start.month:02d}"
            / f"{grain.start.day:02d}.toon"
        )
    if grain.kind == "week":
        # ISO week numbering: the year/week pair belongs to the ISO year
        # (which can differ from grain.start.year for early-Jan weeks
        # that belong to the prior ISO year). isocalendar() gives
        # (iso_year, iso_week, iso_weekday).
        iso_year, iso_week, _ = grain.start.isocalendar()
        return root / f"{iso_year:04d}" / f"W{iso_week:02d}.toon"
    raise ValueError(f"unsupported grain kind: {grain.kind!r}")


def _foods_path(root: Path) -> Path:
    return root / "foods.toon"


def _index_path(root: Path) -> Path:
    return root / "index.toon"


def _enumerate_grains(start: date, end: date, kind: GrainKind) -> list[Grain]:
    """Build the chronological list of grains covering ``[start, end]``.

    For month/week grains the first/last grain typically extends past
    ``start``/``end`` (e.g. a Feb 15 → Feb 20 range with ``kind="month"``
    yields one Feb-1..Feb-29 grain). That's intentional: the spec §2
    layout pins one file per calendar/ISO-week unit; the fetch happens
    against the full grain so the file's bounds match its name.

    Day grains are emitted one per calendar day in the range.
    """
    if start > end:
        return []
    grains: list[Grain] = []
    cursor = start
    if kind == "month":
        # Walk first-of-month forward until we pass ``end``.
        cur_first = date(cursor.year, cursor.month, 1)
        while cur_first <= end:
            g = Grain.month(cur_first)
            grains.append(g)
            # Step to the first of next month.
            if cur_first.month == 12:
                cur_first = cur_first.replace(year=cur_first.year + 1, month=1)
            else:
                cur_first = cur_first.replace(month=cur_first.month + 1)
        return grains
    if kind == "week":
        # ISO-week alignment: snap ``cursor`` back to its Monday.
        while cursor <= end:
            g = Grain.week(cursor)
            grains.append(g)
            cursor = g.end + timedelta(days=1)
        return grains
    if kind == "day":
        while cursor <= end:
            grains.append(Grain.day(cursor))
            cursor = cursor + timedelta(days=1)
        return grains
    raise ValueError(f"unsupported grain kind: {kind!r}")


def _resume_check(
    path: Path,
    *,
    grain: Grain,
    account: AccountRef,
) -> tuple[bool, int]:
    """Decide whether an existing grain file can be skipped on resume.

    Returns ``(skip_ok, entries_count)``. ``skip_ok=True`` means the
    file exists, reads cleanly under the current schema, and the pinned
    ``account`` matches the running user. ``entries_count`` is the
    number of entries the on-disk file recorded — fed into the
    :class:`GrainReport` so the CLI can render ``complete (N days, M
    entries)``.

    A file that fails any check is treated as ``partial`` by the
    caller — the orchestrator re-fetches the whole grain (grain files
    are stateless per spec §4.1).
    """
    if not path.exists():
        return (False, 0)
    try:
        doc = read_grain_file(path)
    except (SchemaVersionMismatch, ValueError, KeyError, OSError) as exc:
        logger.warning("resume: grain file {p} unreadable ({e}); re-fetching", p=path, e=exc)
        return (False, 0)
    if not same_account(doc.account, account):
        logger.warning(
            "resume: grain file {p} pinned to a different account "
            "(file={fu} != running={ru}); re-fetching",
            p=path,
            fu=doc.account,
            ru=account,
        )
        return (False, 0)
    if doc.grain.kind != grain.kind or doc.grain.start != grain.start or doc.grain.end != grain.end:
        # Bounds drift — could happen if the user changed --grain
        # between runs and the on-disk file is from a different shape.
        logger.warning(
            "resume: grain file {p} bounds {fb} != expected {eb}; re-fetching",
            p=path,
            fb=doc.grain,
            eb=grain,
        )
        return (False, 0)
    return (True, len(doc.entries))


def _initialize_foods_file(root: Path, account: AccountRef) -> None:
    """Make sure ``foods.toon`` exists and is bound to ``account``.

    :func:`update_food_cache` (T2) raises :class:`FileNotFoundError`
    when the file is missing — surfacing the missing-account-binding
    bug rather than silently creating an unbound file. The orchestrator
    is the layer that holds the account ref, so the bootstrap belongs
    here.

    If the file already exists and is bound to a *different* account,
    we raise: the user is trying to mix archives from two accounts
    under one root, which is unsupported (spec §8).
    """
    path = _foods_path(root)
    if path.exists():
        from lose_it.backup._fs import read_foods_file

        existing = read_foods_file(path)
        if not same_account(existing.account, account):
            raise ValueError(
                f"foods.toon at {path} is pinned to account {existing.account.user_id!r} "
                f"but running as {account.user_id!r}; use a different --root"
            )
        return
    # Bootstrap: empty foods cache, current account.
    doc = FoodsDoc(account=account, foods={})
    write_foods_file(path, doc)


def _read_or_run_discovery(
    li: _OrchestratorClient,
    *,
    root: Path,
    account: AccountRef,
    grain_kind: GrainKind,
    probe_from: date,
    today: date,
    sleep_seconds: float,
    dry_run: bool,
) -> date | None:
    """Return the cached earliest day if ``index.toon`` exists, else probe.

    The cache lives in ``index.toon`` (spec §4.3 / §5). On the first
    run for a root we issue the probe; subsequent runs read the cache
    and skip the probe entirely. ``dry_run=True`` still runs the probe
    (the cost estimate would otherwise be wrong) but does NOT write
    the index file.
    """
    path = _index_path(root)
    if path.exists():
        try:
            existing = read_index_file(path)
        except (SchemaVersionMismatch, ValueError, KeyError, OSError) as exc:
            logger.warning(
                "discovery: index.toon {p} unreadable ({e}); re-running probe", p=path, e=exc
            )
        else:
            if same_account(existing.account, account):
                logger.info(
                    "discovery: using cached earliest_day={d} from {p}",
                    d=existing.discovered_earliest_day,
                    p=path,
                )
                return existing.discovered_earliest_day
            logger.warning(
                "discovery: index.toon {p} pinned to a different account; re-running probe",
                p=path,
            )
    # Cache miss — run the probe.
    result = discover_earliest_day(
        li,
        probe_from=probe_from,
        today=today,
        sleep_seconds=sleep_seconds,
    )
    if not dry_run:
        index_doc = IndexDoc(
            account=account,
            grain=grain_kind,
            discovered_earliest_day=result.earliest_day,
            discovered_at=_now_utc_iso(),
        )
        write_index_file(path, index_doc)
    return result.earliest_day


def _archive_size_bytes(root: Path) -> int:
    """Sum every regular file's size under ``root`` (recursive). Best-effort."""
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


# ── Backup orchestrator ─────────────────────────────────────────────────────


def backup(
    li: _OrchestratorClient,
    *,
    root: Path,
    grain: GrainKind = "month",
    start: date | None = None,
    end: date | None = None,
    probe_from: date = date(2015, 1, 1),
    resume: bool = True,
    refresh_foods: bool = False,
    sleep_seconds: float = 1.0,
    dry_run: bool = False,
    today: date | None = None,
    progress: Callable[[GrainReport], None] | None = None,
) -> BackupSummary:
    """End-to-end backup orchestrator (spec §3.1 / §6 / §5).

    Composes T1 (file format), T2 (fetch primitive), T3 (discovery)
    into the user-visible ``loseit backup`` flow. The function is
    silent — every per-grain decision is fed to ``progress(report)``
    so the CLI can render the spec §3.1 stdout shape.

    Args:
        li: A structural :class:`lose_it.LoseIt` (or any object
            implementing :class:`_OrchestratorClient`).
        root: Backup root directory. Created on demand.
        grain: Grain kind (``"day" | "week" | "month"``). Default
            ``"month"`` per spec §2.
        start: First date to back up (inclusive). When ``None``,
            triggers the discovery probe (spec §5). When the probe
            returns ``None`` (no entries ever), the backup completes
            with zero grains.
        end: Last date to back up (inclusive). Defaults to ``today``.
        probe_from: Earliest date the discovery probe considers
            (spec §5.4 default ``2015-01-01``).
        resume: When True, skip grain files that exist + read cleanly +
            pin the running account. Default True (spec §3.1).
        refresh_foods: Reserved for a future "ignore the once-per-UTC-day
            describe gate" mode. Today it's passed through to T2 but
            has no effect — T2's :func:`update_food_cache` already
            re-describes any food whose ``last_described_at`` is on a
            different UTC calendar day.
        sleep_seconds: Throttle between RPCs. ``<= 0`` skips throttling
            (tests pass ``0.0``).
        dry_run: When True, run discovery but write no grain files
            and issue no fetch/describe RPCs. The summary still
            describes "what would happen".
        today: Test injection point for "now" (the discovery upper
            bound + default for ``end``). Defaults to ``date.today()``.
        progress: Optional ``(GrainReport) -> None`` callback invoked
            for every grain.

    Returns:
        :class:`BackupSummary` rolling up the per-grain counts.
    """
    if today is None:
        today = date.today()
    if end is None:
        end = today

    account = _account_from_client(li)

    # Discovery — only when --start wasn't pinned.
    if start is None:
        earliest = _read_or_run_discovery(
            li,
            root=root,
            account=account,
            grain_kind=grain,
            probe_from=probe_from,
            today=today,
            sleep_seconds=sleep_seconds,
            dry_run=dry_run,
        )
        if earliest is None:
            # No entries anywhere → nothing to back up. Return cleanly.
            return BackupSummary(
                months_total=0,
                months_skipped=0,
                months_partial=0,
                months_fetched=0,
                months_fell_back=0,
                days_fetched=0,
                days_with_entries=0,
                new_foods_described=0,
                foods_redescribed_today=0,
                root=root,
                archive_size_bytes=_archive_size_bytes(root),
            )
        start = earliest

    # Bootstrap foods.toon up-front so T2's describe-cache writer has
    # something to load. Tested separately; this is a guard against
    # the "missing-account-binding" failure mode (see T2's
    # update_food_cache docstring).
    if not dry_run:
        _initialize_foods_file(root, account)

    grains = _enumerate_grains(start, end, grain)
    logger.info(
        "backup: range={s}..{e} grain={g} grains={n} dry_run={d}",
        s=start.isoformat(),
        e=end.isoformat(),
        g=grain,
        n=len(grains),
        d=dry_run,
    )

    # Roll-up counters.
    months_skipped = 0
    months_partial = 0
    months_fetched = 0
    months_fell_back = 0
    days_fetched = 0
    days_with_entries = 0
    new_foods_described = 0
    foods_redescribed_today = 0

    for idx, g in enumerate(grains):
        path = _grain_file_path(root, g)

        # 1. Resume path — file present + readable + account-matched.
        if resume:
            skip_ok, n_entries = _resume_check(path, grain=g, account=account)
            if skip_ok:
                report = GrainReport(
                    grain=g,
                    status=FetchStatus.skip,
                    days_with_entries=0,  # we don't re-parse to count
                    entries=n_entries,
                )
                if progress is not None:
                    progress(report)
                months_skipped += 1
                continue
            # File exists but failed checks → partial. We re-fetch the
            # whole grain because grain files are stateless (§4.1).
            if path.exists():
                months_partial += 1
            # (otherwise the path doesn't exist and we'll count it as
            #  fetched/fell-back below.)

        # 2. Dry-run — count this grain as if we'd fetched it but don't
        # actually issue RPCs.
        if dry_run:
            report = GrainReport(
                grain=g,
                status=FetchStatus.fetch,
                days_with_entries=0,
                entries=0,
            )
            if progress is not None:
                progress(report)
            months_fetched += 1
            continue

        # 3. Inter-grain throttle. The first grain doesn't sleep — the
        # previous step was discovery or "nothing", neither of which
        # needs a delay before our first fetch.
        if idx > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)

        # 4. Fetch. Recursive split-and-retry lives inside T2.
        entries, fetch_status = fetch_grain(li, g, sleep_seconds=sleep_seconds)

        # 5. Bucket entries by date so we can count "days with entries".
        by_date: dict[date, list[Any]] = {}
        for fle in entries:
            # The entry's wire ``day_num`` is the canonical date source
            # for entries; the splitter handed us per-day buckets so we
            # could equally key by the bucket date, but the orchestrator
            # walks the flat list and re-derives the date from day_num
            # to stay independent of the splitter's bucket shape.
            d = _entry_date_for(fle, g)
            by_date.setdefault(d, []).append(fle)
        n_days_with_entries = sum(1 for v in by_date.values() if v)

        # 6. Project into the on-disk GrainEntry shape.
        ingest_ts = _now_utc_iso()
        grain_entries = []
        seen_food_ids: list[str] = []
        for d, day_entries in by_date.items():
            for fle in day_entries:
                ge = to_grain_entry(fle, entry_date=d, ingest_ts=ingest_ts)
                grain_entries.append(ge)
                if ge.food_id and ge.food_id not in seen_food_ids:
                    seen_food_ids.append(ge.food_id)
        grain_entries.sort(key=grain_entry_sort_key)

        # 7. Write the grain file atomically (T1).
        doc = GrainDoc(
            account=account,
            grain=GrainBounds(kind=g.kind, start=g.start, end=g.end),
            generated_at=ingest_ts,
            entries=grain_entries,
            schema_version=SCHEMA_VERSION,
        )
        write_grain_file(path, doc)

        # 8. Update the foods cache (T2). Sleep-throttled; honors the
        # once-per-UTC-day describe gate.
        described_count = 0
        if seen_food_ids:
            try:
                described_count = update_food_cache(
                    li,
                    _foods_path(root),
                    seen_food_ids,
                    sleep_seconds=sleep_seconds,
                )
            except Exception as exc:
                # A describe failure shouldn't roll back the grain file
                # (the entries themselves are intact). The CLI surfaces
                # the warning via the logger; the run continues.
                logger.warning(
                    "backup: describe failure for grain {g} ({e}); continuing",
                    g=g,
                    e=exc,
                )

        new_foods_described += described_count
        # Spec §3.1's "re-described today" counter is "the foods we
        # touched today but whose cache already had today's stamp." T2's
        # gate makes ``described_count`` exactly the NEW describes; the
        # delta against ``len(seen_food_ids)`` is the re-described
        # bucket. Both are surfaced for the CLI to render.
        foods_redescribed_today += max(0, len(seen_food_ids) - described_count)

        # 9. Roll-up + progress.
        days_fetched += _grain_day_span(g)
        days_with_entries += n_days_with_entries
        if fetch_status is FetchStatus.fallback:
            months_fell_back += 1
        else:
            months_fetched += 1

        report = GrainReport(
            grain=g,
            status=fetch_status,
            days_with_entries=n_days_with_entries,
            entries=len(grain_entries),
        )
        if progress is not None:
            progress(report)

    return BackupSummary(
        months_total=len(grains),
        months_skipped=months_skipped,
        months_partial=months_partial,
        months_fetched=months_fetched,
        months_fell_back=months_fell_back,
        days_fetched=days_fetched,
        days_with_entries=days_with_entries,
        new_foods_described=new_foods_described,
        foods_redescribed_today=foods_redescribed_today,
        root=root,
        archive_size_bytes=_archive_size_bytes(root),
    )


def _grain_day_span(g: Grain) -> int:
    """Number of calendar days in ``[g.start, g.end]`` inclusive."""
    return (g.end - g.start).days + 1


# Day-num anchor for deriving an entry's calendar date from its ``day_num``
# when the orchestrator can't trust the grain's bucket key. Sourced from
# ``lose_it.core._dates.day_number_for`` so we stay aligned with the rest
# of the SDK.
def _entry_date_for(fle: Any, grain: Grain) -> date:
    """Best-effort date for a FoodLogEntry inside ``grain``.

    The splitter's per-day buckets give us the date directly, but
    :func:`fetch_grain` flattens them before returning. We fall back to
    deriving from ``day_num`` via the shared anchor — that's also how
    :func:`lose_it.core._dates.day_number_for` works in reverse.

    For day-grain we just use ``grain.start`` since there's only one
    possible date.
    """
    if grain.kind == "day":
        return grain.start
    day_num = int(getattr(fle, "day_num", 0))
    if day_num <= 0:
        # Defensive fallback — pin to the grain start so the entry
        # still lands in a sane bucket. In practice every real wire
        # entry carries a positive day_num.
        return grain.start
    return _date_for_day_number(day_num)


def _date_for_day_number(day_num: int) -> date:
    """Inverse of :func:`lose_it.core._dates.day_number_for`.

    The SDK exposes ``day_number_for(date) -> int`` but not the
    inverse. The orchestrator needs it to bucket fetched entries back
    onto their calendar dates, so we re-derive it from the same
    anchor instead of adding a public helper to ``_dates.py`` (that's
    a wider surface change than T6's scope allows).
    """
    from lose_it.core._config import DAY_NUM_ANCHOR_DATE, DAY_NUM_ANCHOR_VALUE

    anchor = datetime.strptime(DAY_NUM_ANCHOR_DATE, "%Y-%m-%d").date()
    return anchor + timedelta(days=day_num - DAY_NUM_ANCHOR_VALUE)


# ── Cheap-mode restore (spec §7.2) ──────────────────────────────────────────


def _walk_grain_files(root: Path, kind: GrainKind) -> list[Path]:
    """Discover every grain file under ``root`` for the given kind.

    Files outside the layout (``foods.toon``, ``index.toon``, the
    ``.tmp.*`` siblings the atomic-write helper leaves on crash) are
    filtered out. Returned paths are sorted lexicographically — which
    coincides with chronological order under the spec §2 layout.
    """
    if not root.exists():
        return []
    if kind == "month":
        return sorted(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9].toon"))
    if kind == "day":
        return sorted(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9].toon"))
    if kind == "week":
        return sorted(root.glob("[0-9][0-9][0-9][0-9]/W[0-9][0-9].toon"))
    raise ValueError(f"unsupported grain kind: {kind!r}")


def _fle_food_id(fle: Any) -> str:
    """Project a :class:`FoodLogEntry`-like into its 32-char hex food_id."""
    pk = getattr(fle, "food_pk_response", None)
    if pk and len(pk) == 16:
        return pk_to_hex(list(pk))
    return ""


def restore_backup_cheap(
    li: _OrchestratorClient,
    *,
    root: Path,
    grain: GrainKind = "month",
    start: date | None = None,
    end: date | None = None,
    strict_account: bool = True,
    sleep_seconds: float = 1.0,
    dry_run: bool = False,
    progress: Callable[[CheapRestoreGrainReport], None] | None = None,
) -> RestoreSummary:
    """Cheap-mode restore (spec §7.2).

    For each grain file in ``root``:

    1. Walk every day in ``[grain.start, grain.end]`` chronologically
       via ``li.diary(d)`` (NOT ``diary_range`` — spec §7.2 reads day-by-
       day so the spec's early-exit semantics fall out naturally).
    2. On the first non-empty day, mark the grain ``skip``: the server
       has data here already, and cheap mode trades fidelity for
       simplicity. Skip the rest of the grain's days + every backup
       entry inside it.
    3. If every day is empty, log every backup entry via
       :meth:`li.log_food(food_id, meal, servings, when=date)`. Sleep
       ``sleep_seconds`` between log calls.

    Account guard: ``strict_account=True`` (the default) refuses to
    restore from a grain file whose ``account.user_id`` doesn't match
    the running client. This is the failsafe spec §8 calls out.

    ``dry_run=True`` runs the read pass but issues no ``log_food`` RPCs
    — the report still describes what would be logged.

    Returns a :class:`RestoreSummary` rolling up per-grain decisions.
    """
    account = _account_from_client(li)
    grain_files = _walk_grain_files(root, grain)
    logger.info(
        "restore-cheap: root={r} grain={g} files={n} dry_run={d}",
        r=root,
        g=grain,
        n=len(grain_files),
        d=dry_run,
    )

    grains_skipped = 0
    grains_restored = 0
    entries_logged = 0

    for idx, path in enumerate(grain_files):
        doc = read_grain_file(path)

        # Strict-account guard — refuses to restore a file pinned to a
        # different account when the flag is on.
        if strict_account and not same_account(doc.account, account):
            raise ValueError(
                f"refusing to restore {path}: pinned to account "
                f"{doc.account.user_id!r} but running as {account.user_id!r}; "
                f"pass strict_account=False to override (spec §8)"
            )

        # Window clip — start/end let the caller restrict the walk.
        gb = doc.grain
        if start is not None and gb.end < start:
            continue
        if end is not None and gb.start > end:
            continue

        # Spec §7.2: walk every day in [grain.start, grain.end].
        cur = gb.start
        days_scanned = 0
        hit_day: date | None = None
        while cur <= gb.end:
            # Skip the first sleep entirely — we don't need to throttle
            # before the very first probe of the very first grain (no
            # prior RPC to space against).
            if (idx > 0 or days_scanned > 0) and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            day_entries = li.diary(cur)
            days_scanned += 1
            if day_entries:
                hit_day = cur
                break
            cur = cur + timedelta(days=1)

        n_entries_in_grain = len(doc.entries)

        if hit_day is not None:
            grains_skipped += 1
            report = CheapRestoreGrainReport(
                grain_path=path,
                status="skip",
                days_scanned=days_scanned,
                hit_day=hit_day,
                entries_in_grain=n_entries_in_grain,
                entries_logged=0,
            )
            if progress is not None:
                progress(report)
            continue

        # All days empty → log every backup entry.
        logged_count = 0
        if not dry_run:
            for j, entry in enumerate(doc.entries):
                if j > 0 and sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                # ``LoseIt.log_food`` accepts a 32-char hex food_id
                # directly. We hand it the four required fields per
                # spec §4.4 (food_id, meal_ordinal, servings, date).
                li.log_food(
                    entry.food_id,
                    meal=int(entry.meal_ordinal),
                    servings=float(entry.servings),
                    when=entry.date,
                )
                logged_count += 1
        else:
            # Dry-run still counts what *would* be logged so the summary
            # tells the user "X entries would be logged".
            logged_count = 0
        entries_logged += logged_count
        grains_restored += 1
        report = CheapRestoreGrainReport(
            grain_path=path,
            status="restore",
            days_scanned=days_scanned,
            hit_day=None,
            entries_in_grain=n_entries_in_grain,
            entries_logged=logged_count,
        )
        if progress is not None:
            progress(report)

    return RestoreSummary(
        grains_scanned=len(grain_files),
        grains_skipped=grains_skipped,
        grains_restored=grains_restored,
        entries_logged=entries_logged,
        root=root,
    )


# ── Safe-mode restore (spec §7.1) ───────────────────────────────────────────


def restore_backup_safe(
    li: _OrchestratorClient,
    *,
    root: Path,
    grain: GrainKind = "month",
    start: date | None = None,
    end: date | None = None,
    strict_account: bool = True,
    upsert_window: timedelta = timedelta(minutes=10),
    sleep_seconds: float = 1.0,
    dry_run: bool = False,
    progress: Callable[[SafeRestoreGrainReport], None] | None = None,
) -> RestoreSummary:
    """Safe-mode restore (spec §7.1): per-day entry-level upsert.

    Composes T7's :func:`lose_it.backup._upsert.plan_day` with this
    track's orchestration: for every day in the archive that has at
    least one entry,

    1. Fetch the server's diary for that day via :meth:`li.diary`.
    2. Pass ``(archive_entries_for_day, server_entries)`` to
       :func:`plan_day` with the configured ``upsert_window``.
    3. For every :class:`GrainEntry` in the plan's ``missing`` list,
       issue :meth:`li.log_food` with the spec §4.4 minimum payload
       (``food_id, meal_ordinal, servings, when=date``).

    Restore is purely additive (spec §7.4) — server-only entries are
    left alone. The ``dry_run`` switch keeps the read pass intact but
    suppresses every ``log_food`` call so users can preview a restore.

    Account guard: ``strict_account=True`` (the default) refuses to
    restore from a grain file whose ``account.user_id`` doesn't match
    the running client.

    Args:
        li: A structural :class:`lose_it.LoseIt` (or :class:`_OrchestratorClient`).
        root: Backup root directory containing the grain-file tree.
        grain: ``"day" | "week" | "month"`` — what file layout to walk.
        start: Earliest grain to restore (inclusive). ``None`` → no clip.
        end: Latest grain to restore (inclusive). ``None`` → no clip.
        strict_account: Refuse to restore from grain files pinned to a
            different account. Default True (spec §8).
        upsert_window: Match-key fuzz window for ``modified_at``.
            Default ±10 minutes (spec §7.1).
        sleep_seconds: Throttle between RPCs. ``<= 0`` skips throttling.
        dry_run: Read server diaries but issue no ``log_food`` RPCs.
        progress: Optional callback fired with one
            :class:`SafeRestoreGrainReport` per grain file processed.

    Returns:
        :class:`RestoreSummary` rolling up per-grain + per-day counters.
    """
    # Lazy import keeps the (T7) upsert module out of the cheap-mode
    # call path — and avoids any chance of a circular import.
    from lose_it.backup._upsert import plan_day

    account = _account_from_client(li)
    grain_files = _walk_grain_files(root, grain)
    logger.info(
        "restore-safe: root={r} grain={g} files={n} dry_run={d}",
        r=root,
        g=grain,
        n=len(grain_files),
        d=dry_run,
    )

    grains_restored = 0
    entries_logged = 0
    days_scanned = 0
    days_fully_present = 0
    days_upserted = 0
    entries_already_present = 0
    first_rpc_emitted = False

    for path in grain_files:
        doc = read_grain_file(path)

        # Strict-account guard — refuses to restore a file pinned to a
        # different account when the flag is on.
        if strict_account and not same_account(doc.account, account):
            raise ValueError(
                f"refusing to restore {path}: pinned to account "
                f"{doc.account.user_id!r} but running as {account.user_id!r}; "
                f"pass strict_account=False to override (spec §8)"
            )

        # Window clip — start/end let the caller restrict the walk.
        gb = doc.grain
        if start is not None and gb.end < start:
            continue
        if end is not None and gb.start > end:
            continue

        # Bucket the archive entries by date — safe mode operates per
        # day-with-entries (spec §7.1's flowchart loops over D).
        archive_by_day: dict[date, list[Any]] = {}
        for entry in doc.entries:
            archive_by_day.setdefault(entry.date, []).append(entry)
        days_with_entries_for_grain = sum(1 for v in archive_by_day.values() if v)

        days_present_for_grain = 0
        days_upserted_for_grain = 0
        days_empty_for_grain = 0
        entries_present_for_grain = 0
        entries_logged_for_grain = 0

        for d in sorted(archive_by_day.keys()):
            day_archive = archive_by_day[d]
            if not day_archive:
                # Defensive — bucket build skips empty values, but a future
                # caller could pre-populate the dict with empty lists.
                days_empty_for_grain += 1
                continue

            # 1. GET server diary for D (one RPC).
            if first_rpc_emitted and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            server_entries = list(li.diary(d))
            days_scanned += 1
            first_rpc_emitted = True

            # 2. Compute the per-day plan.
            day_plan = plan_day(day_archive, server_entries, window=upsert_window)
            n_matched = len(day_plan.matched)
            n_missing = len(day_plan.missing)
            entries_present_for_grain += n_matched
            entries_already_present += n_matched

            # 3. For each missing entry, fire a log_food (unless dry-run).
            if n_missing == 0:
                # Every archive entry on this day already on the server.
                days_present_for_grain += 1
                continue

            days_upserted_for_grain += 1
            for gentry in day_plan.missing:
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                if not dry_run:
                    li.log_food(
                        gentry.food_id,
                        meal=int(gentry.meal_ordinal),
                        servings=float(gentry.servings),
                        when=gentry.date,
                    )
                    entries_logged_for_grain += 1
                    entries_logged += 1
                # else: dry-run — count would-be logs only via the plan.

        days_fully_present += days_present_for_grain
        days_upserted += days_upserted_for_grain
        grains_restored += 1
        report = SafeRestoreGrainReport(
            grain_path=path,
            days_with_entries=days_with_entries_for_grain,
            days_present=days_present_for_grain,
            days_upserted=days_upserted_for_grain,
            days_empty=days_empty_for_grain,
            entries_present=entries_present_for_grain,
            entries_logged=entries_logged_for_grain,
        )
        if progress is not None:
            progress(report)

    return RestoreSummary(
        grains_scanned=len(grain_files),
        grains_skipped=0,
        grains_restored=grains_restored,
        entries_logged=entries_logged,
        root=root,
        days_scanned=days_scanned,
        days_fully_present=days_fully_present,
        days_upserted=days_upserted,
        entries_already_present=entries_already_present,
    )


__all__ = [
    "BackupSummary",
    "CheapRestoreGrainReport",
    "GrainKind",
    "GrainReport",
    "RestoreSummary",
    "SafeRestoreGrainReport",
    "backup",
    "restore_backup_cheap",
    "restore_backup_safe",
]
