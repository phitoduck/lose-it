"""High-level Lose It! SDK client.

:class:`LoseIt` owns the HTTP session + account config and exposes one
method per user-facing capability (search, log, diary, delete, describe,
login bootstrap). Each method composes pure helpers from
:mod:`.core._portion` / :mod:`.core._login_flow` with the low-level RPC
functions in :mod:`.core.foods` / :mod:`.core.entries` /
:mod:`.core.daily` / :mod:`.core.init`.

The class is a thin façade — the goal is *call site ergonomics*::

    from lose_it import LoseIt

    with LoseIt.from_env() as li:
        results = li.search("tortilla")
        logged = li.log_food(results[0], meal="lunch", servings=1.0)
        for entry in li.diary():
            print(entry.food_name, entry.servings)
        li.delete_entry(entry)

…compared to the old style of threading ``client.http`` through
module-level functions and reproducing the portion-resolution +
day_key-lookup glue at every call site.

:class:`Client` is the existing low-level handle (Config + HttpClient)
that the module-level RPC functions in :mod:`lose_it.core` take as a
first argument. Kept here unchanged so existing code keeps working; once
:class:`LoseIt` is implemented, ``Client`` will become an alias.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ._logging import logger
from .core import auth as _auth
from .core import daily as _daily
from .core import entries as _entries
from .core import foods as _foods
from .core._config import Config
from .core._dates import day_number_for
from .core._http import HttpClient
from .core._ids import hex_to_pk
from .core._login_flow import derive_config_values
from .core._portion import resolve_portion, scaled_calories
from .core._settings import DEFAULT_CONFIG_FILE, write_yaml_config
from .core.auth import (
    DEFAULT_TOKEN_FILE,
    decode_jwt_exp,
    is_token_expired,
    refresh_token_from_browser,
    save_token,
)
from .core.init import get_daydate_key, get_init_day_keys
from .enums import MealType, ServingUnit
from .models import (
    CrossClassConversion,
    FoodDescription,
    FoodLogEntry,
    FoodSearchResult,
    LoggedFood,
    LoginResult,
    PrimaryServing,
    UnsavedFoodLogEntry,
)
from .trash import (
    DeleteResult,
    DeleteSafetyError,
    LocalFileTrashSink,
    TrashReceipt,
    TrashSink,
    default_trash_file,
)

# Lose It's signin URL — surfaced in LoginResult for the CLI to display.
_SIGNIN_URL = "https://www.loseit.com/"

# Sentinel for ``delete_entry(trash_sink=...)`` that distinguishes
# "caller did not pass the kwarg" (use the default LocalFileTrashSink)
# from "caller passed None" (explicit opt-out, gated by
# acknowledge_no_trash). ``None`` itself can't carry both meanings.
_DEFAULT_SINK_SENTINEL: object = object()


__all__ = ["Client", "LoseIt"]


class Client:
    """Low-level handle: account config + authenticated httpx session.

    Used by the module-level RPC functions in :mod:`lose_it.core`. Most
    callers should reach for :class:`LoseIt` (high-level) instead.
    """

    def __init__(
        self,
        config: Config,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = config
        self.http = HttpClient(config, token, transport=transport)

    @classmethod
    def from_env(
        cls,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        **config_overrides: Any,
    ) -> Client:
        """Build a Client from the layered config (CLI > env > YAML > defaults).

        ``token`` and any ``LOSEIT_*`` settings are resolved from the same
        layered sources via :meth:`Config.from_env`. If a ``token`` kwarg
        is passed explicitly it wins; otherwise the resolved
        ``config.token`` is used; otherwise the token file at
        ``config.token_file`` is read.
        """
        logger.debug(
            "Client.from_env: overrides={ov}",
            ov={k: v for k, v in config_overrides.items() if v is not None},
        )
        config = Config.from_env(**config_overrides)
        if token is None:
            token = config.token or _auth.load_token(config.token_file)
        logger.info(
            "Client.from_env: user={u!r} hours_from_gmt={h} permutation={p}",
            u=config.user_name,
            h=config.hours_from_gmt,
            p=config.strong_name,
        )
        return cls(config, token, transport=transport)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class LoseIt:
    """High-level handle. Holds :class:`Config` + :class:`HttpClient` and
    exposes one method per user-facing capability.

    Construct via :meth:`from_env` (the layered CLI > env > YAML >
    defaults loader). Direct construction is supported for tests; pass a
    pre-built :class:`Config` and JWT.
    """

    config: Config
    http: HttpClient

    def __init__(
        self,
        config: Config,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.http = HttpClient(config, token, transport=transport)

    # ── Lifecycle ───────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        **config_overrides: Any,
    ) -> LoseIt:
        """Build a client from the layered config (CLI > env > YAML > defaults).

        ``token`` and any ``LOSEIT_*`` settings are resolved from the same
        layered sources via :meth:`Config.from_env`. If a ``token`` kwarg
        is passed explicitly it wins; otherwise the resolved
        ``config.token`` is used; otherwise the JWT at ``config.token_file``
        is read.
        """
        logger.debug(
            "LoseIt.from_env: overrides={ov}",
            ov={k: v for k, v in config_overrides.items() if v is not None},
        )
        config = Config.from_env(**config_overrides)
        if token is None:
            token = config.token or _auth.load_token(config.token_file)
        logger.info(
            "LoseIt.from_env: user={u!r} hours_from_gmt={h} permutation={p}",
            u=config.user_name,
            h=config.hours_from_gmt,
            p=config.strong_name,
        )
        return cls(config, token, transport=transport)

    def close(self) -> None:
        """Close the underlying httpx session. Idempotent."""
        self.http.close()

    def __enter__(self) -> LoseIt:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Identity ────────────────────────────────────────────────────────

    def whoami(self) -> Config:
        """Return the resolved :class:`Config` (alias for ``self.config``).

        Exists so callers can write ``li.whoami()`` symmetrically with
        the ``loseit whoami`` command, even though it makes no RPC.
        """
        return self.config

    # ── Food lookup ─────────────────────────────────────────────────────

    def search(self, query: str) -> list[FoodSearchResult]:
        """Search the LoseIt food database. Returns up to ~15 results.

        Thin wrapper over :func:`lose_it.core.foods.search`; folded
        here so callers can use ``li.search(...)`` instead of
        ``foods.search(li.http, ...)``.
        """
        return _foods.search(self.http, query)

    def get_food(self, food_id: str | list[int]) -> FoodSearchResult:
        """Look up a food by ID (hex string or raw 16-byte list).

        Accepts the lowercase-hex form exposed by ``loseit search``'s
        ``Food ID`` column / JSON ``food_id`` field, or the raw
        ``pk_bytes`` list returned by :meth:`search`. Hex strings get
        validated/decoded via :func:`lose_it.core._ids.hex_to_pk`.
        """
        pk_bytes = hex_to_pk(food_id) if isinstance(food_id, str) else list(food_id)
        return _foods.get_food(self.http, pk_bytes)

    def get_food_template(self, food: FoodSearchResult | str | list[int]) -> UnsavedFoodLogEntry:
        """Fetch the unsaved-entry template for a food (nutrient + serving sizes).

        Accepts either a :class:`FoodSearchResult` (skips the ``getFood``
        round-trip) or a food id (in which case ``getFood`` is called
        first to resolve the name/brand the unsaved-entry RPC needs).
        """
        if not isinstance(food, FoodSearchResult):
            food = self.get_food(food)
        return _foods.get_unsaved_food_log_entry(self.http, food)

    def describe_food(self, food_id: str) -> FoodDescription:
        """Return the full nutrient + serving profile for one food.

        Internally: ``get_food`` → ``get_unsaved_food_log_entry`` →
        synthesize a :class:`FoodDescription`. Cheaper than calling
        those two RPCs by hand and remembering which fields to project.
        """
        return self._describe_one(food_id)

    def _describe_one(self, food_id: str) -> FoodDescription:
        """Internal: single-food describe, also used as the per-task body
        of :meth:`describe_foods`. Letting both call paths share it keeps
        the projection shape pinned in one place.
        """
        pk_bytes = hex_to_pk(food_id)
        food = _foods.get_food(self.http, pk_bytes)
        unsaved = _foods.get_unsaved_food_log_entry(self.http, food)
        return FoodDescription(
            food_id=food_id,
            name=unsaved.name,
            brand=unsaved.brand,
            category=unsaved.category,
            primary_serving=PrimaryServing(
                ordinal=unsaved.food_measure_ordinal,
                unit=unsaved.food_measure_unit,
                canonical_per_serving=unsaved.canonical_per_serving,
                native_qty_per_serving=unsaved.native_qty_per_serving,
            ),
            cross_class_conversion=CrossClassConversion(
                per_serving_g=unsaved.per_serving_g,
                per_serving_ml=unsaved.per_serving_ml,
            ),
            nutrients_per_serving=dict(unsaved.nutrients_by_label),
            raw_nutrients_by_ord=dict(unsaved.nutrients),
        )

    def describe_foods(self, food_ids: list[str]) -> list[FoodDescription]:
        """Describe multiple foods concurrently.

        Each ID is fetched in parallel via ``asyncio.to_thread`` over the
        sync HTTP client — N foods take ~max(per-request-latency) rather
        than sum. Order of the returned list matches the input order.
        Exceptions from any single fetch propagate; for a "best-effort"
        version, call :meth:`describe_food` in a loop with try/except.
        """

        async def _gather() -> list[FoodDescription]:
            return await asyncio.gather(
                *(asyncio.to_thread(self._describe_one, fid) for fid in food_ids)
            )

        return asyncio.run(_gather())

    # ── Diary CRUD ──────────────────────────────────────────────────────

    def diary(self, when: date | None = None) -> list[FoodLogEntry]:
        """List the day's diary entries. ``when=None`` → today."""
        if when is None:
            when = date.today()
        return _daily.get_daily_details(self.http, when)

    def diary_range(
        self,
        start: date,
        end: date,
    ) -> dict[date, list[FoodLogEntry]]:
        """Bulk diary fetch via ``getDailyDetailsIncludingPendingForDateRange``.

        Returns a per-day map covering every date in ``[start, end]``
        inclusive — days with no entries are still present with an empty
        list.

        On the wire this issues exactly **one** range RPC per call. The
        first time the method is invoked on a given client instance it
        also issues one ``getInitializationData`` RPC to bootstrap the
        day-key cache — subsequent calls reuse the cached window. So a
        backup loop that walks 365 calendar days as 52 weekly slices
        spends 1 init + 52 range RPCs, never per-day RPCs.

        Raises :class:`lose_it.core.daily.TooMuchData` on
        413 / 429 / 5xx responses or oversize ``//EX`` envelopes; the
        backup primitive (T2) catches this and recurses into a smaller
        grain.
        """
        day_keys = self._range_day_keys(start, end)
        return _daily.get_daily_details_range(
            self.http,
            start=start,
            end=end,
            day_keys=day_keys,
        )

    # Per-instance day_key cache for diary_range. Populated lazily on
    # first call by issuing a single getInitializationData RPC. The server
    # accepts ``_FALLBACK_DAY_KEY`` for anything outside the init window,
    # so once we've made the bootstrap call we don't need a second one
    # for later ranges that happen to fall outside the cached window.
    _day_key_cache: dict[int, str]
    _day_key_cache_loaded: bool

    def _range_day_keys(self, start: date, end: date) -> dict[int, str]:
        """Resolve the ``(start, end)`` day_keys, bootstrapping on demand.

        On the first call this issues exactly one
        ``getInitializationData`` RPC, parses the entire day-key window
        out of the response, and caches it on the instance. Subsequent
        calls reuse the cached window — no extra init RPCs, even if the
        new range falls outside the cached window (the server accepts
        the ``ZZZZZZZ`` fallback for any day_num it doesn't recognize).
        """
        if not getattr(self, "_day_key_cache_loaded", False):
            self._day_key_cache = get_init_day_keys(self.http)
            self._day_key_cache_loaded = True
        return dict(self._day_key_cache)

    def log_food(
        self,
        food: FoodSearchResult | str,
        meal: MealType | str | int = MealType.snacks,
        servings: float = 1.0,
        *,
        serving_amount: float | None = None,
        serving_unit: ServingUnit | str | None = None,
        when: date | None = None,
        dry_run: bool = False,
    ) -> LoggedFood:
        """Log a food to a meal. Pure-helper orchestrator.

        Args:
            food: Either a :class:`FoodSearchResult` (e.g. from
                :meth:`search`) or a 32-char hex food ID. Hex IDs trigger
                a ``getFood`` round-trip to resolve the food name.
            meal: A :class:`MealType` member or anything
                :meth:`MealType.parse` accepts (case-insensitive name,
                the ``"snack"`` alias, or the raw ordinal ``0..3``).
            servings: Raw canonical multiplier. Mutually exclusive with
                ``serving_amount``/``serving_unit``.
            serving_amount: Quantity in ``serving_unit`` (e.g. ``490``).
                Must be passed together with ``serving_unit``.
            serving_unit: A :class:`ServingUnit` member or its string
                form (``"mL"``, ``"g"``, ``"cup"``, …). Common aliases
                like ``"cups"`` / ``"milliliter"`` are also resolved.
            when: Target date. ``None`` → today.
            dry_run: Skip the ``updateFoodLogEntry`` RPC and the
                day-key lookup; still returns a :class:`LoggedFood` with
                ``dry_run=True``.

        Returns:
            :class:`LoggedFood` carrying the resolved food, meal, portion
            shape, and scaled calorie total.

        Raises:
            ValueError: ``meal`` not recognized.
            PortionError: invalid portion-size combination (see
                :func:`lose_it.core._portion.resolve_portion`).
        """
        meal_type = MealType.parse(meal)
        if when is None:
            when = date.today()
        if not isinstance(food, FoodSearchResult):
            food = self.get_food(food)
        unsaved = _foods.get_unsaved_food_log_entry(self.http, food)
        portion = resolve_portion(
            unsaved,
            servings=servings,
            serving_amount=serving_amount,
            serving_unit=serving_unit,
        )
        calories = scaled_calories(unsaved, portion.canonical_servings)

        if not dry_run:
            day_num = day_number_for(when)
            day_key = get_daydate_key(self.http, day_num) or ""
            _entries.log_food(
                self.http,
                unsaved,
                int(meal_type),
                day_key,
                day_num,
                portion.canonical_servings,
                measure_ord_override=portion.measure_ord_override,
                quantity_in_chosen_unit=portion.quantity_in_chosen_unit,
                conversion_factor=portion.conversion_factor,
            )

        return LoggedFood(
            food=food,
            meal_ordinal=int(meal_type),
            meal_name=meal_type.name,
            when=when.isoformat(),
            canonical_servings=portion.canonical_servings,
            portion_amount=portion.display_amount,
            portion_unit=portion.display_unit,
            calories=calories,
            dry_run=dry_run,
        )

    def delete_entry(
        self,
        entry: FoodLogEntry,
        *,
        trash_sink: TrashSink | None = _DEFAULT_SINK_SENTINEL,  # type: ignore[assignment]
        acknowledge_no_trash: bool = False,
        confirm: bool = False,
    ) -> DeleteResult:
        """Delete a diary entry via a trash sink + the wire call.

        Every delete writes a recoverable trash record **first** —
        only after the sink confirms it captured the entry does the
        ``deleteFoodLogEntry`` RPC fire. See ``docs/backup-spec.md``
        §9 for the full rationale.

        Args:
            entry: The diary entry to remove.
            trash_sink: Where to stash the entry before deletion.

                - **Default (sentinel):** a fresh
                  :class:`LocalFileTrashSink` writing to
                  ``~/.local/share/loseit/trash.jsonl`` (mode ``0o600``).
                - ``None``: explicit opt-out. Requires
                  ``acknowledge_no_trash=True`` or :class:`DeleteSafetyError`
                  is raised.
                - Any object implementing the :class:`TrashSink` protocol
                  (``stash(entry) -> TrashReceipt``) is used as-is.
            acknowledge_no_trash: Required when ``trash_sink is None`` —
                a no-op otherwise. Documents that the caller knows the
                entry will be unrecoverable.
            confirm: Reserved for future "type the food name to confirm"
                flow inside the SDK. Currently unused; the CLI handles
                interactive confirmation today.

        Returns:
            :class:`DeleteResult` carrying the entry's JSON projection,
            the receipts from every sink that absorbed it (zero receipts
            on explicit opt-out), and the UTC ISO timestamp of the wire
            delete.

        Raises:
            DeleteSafetyError: ``trash_sink is None`` and
                ``acknowledge_no_trash`` is False.
            Exception: Anything the sink raises during ``stash``. The
                wire delete is NOT sent in that case — the entry is still
                on the server.
        """
        # Late-resolve the sentinel so we use a fresh sink per call (the
        # default path expands ``~`` and accepts any LOSEIT_USER-driven
        # account name the caller passes through).
        resolved_sink: TrashSink | None
        if trash_sink is _DEFAULT_SINK_SENTINEL:
            resolved_sink = LocalFileTrashSink(user_name=self.config.user_name or "")
        else:
            resolved_sink = trash_sink  # type: ignore[assignment]

        receipts: list[TrashReceipt] = []
        if resolved_sink is None:
            if not acknowledge_no_trash:
                raise DeleteSafetyError("trash_required")
        else:
            # Any exception from ``stash`` propagates and skips the wire
            # call — that's the whole invariant.
            receipts.append(resolved_sink.stash(entry))

        _entries.delete(self.http, entry)

        return DeleteResult(
            entry=entry.to_dict(),
            trash_receipts=receipts,
            deleted_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        )

    def restore_trash(
        self,
        *,
        trash_file: Path | None = None,
        line: int | None = None,
        keep: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-log a record from the trash file.

        Reads one line from ``trash_file`` (defaults to
        ``~/.local/share/loseit/trash.jsonl``), re-issues
        :meth:`log_food` with the minimum payload set
        (``food_id``, ``meal``, ``servings``, ``when``), and either
        consumes the line (default) or keeps it.

        Args:
            trash_file: Source trash file. ``None`` resolves the default.
            line: 1-based line number to restore. ``None`` → last line.
            keep: When True, leave the line on disk after restoring.
            dry_run: When True, return the plan without re-logging or
                modifying the file.

        Returns:
            A small dict carrying the plan + outcome — used by the CLI
            to render the BDD-specified stdout. Keys:

            - ``trash_file``: resolved path.
            - ``line_no``: which 1-based line was acted on.
            - ``total_lines``: line count of the file before restore.
            - ``is_last``: ``line_no == total_lines``.
            - ``food_id`` / ``food_name`` / ``meal`` / ``date`` /
              ``servings``: the record's projection.
            - ``logged``: the :class:`LoggedFood` from ``log_food`` (or
              ``None`` for dry-run / unresolved food).
            - ``consumed``: True iff the line was removed from the file.
            - ``dry_run``: echoes the input.
            - ``keep``: echoes the input.
        """
        path = trash_file if trash_file is not None else default_trash_file()
        text = path.read_text(encoding="utf-8")
        # ``splitlines()`` drops the trailing newline if present — we use
        # ``keepends=False`` and re-assemble below.
        lines = text.splitlines()
        if not lines:
            raise ValueError(f"trash file {path} is empty")

        total = len(lines)
        line_no = line if line is not None else total
        if not 1 <= line_no <= total:
            raise ValueError(f"--line {line_no} out of range (file has {total} lines)")
        record_raw = lines[line_no - 1]
        record = json.loads(record_raw)
        entry_dict = record.get("entry", {})

        food_id = entry_dict.get("food_id") or ""
        food_name = entry_dict.get("food_name", "")
        meal_name = entry_dict.get("meal", "snacks")
        date_str = entry_dict.get("date") or ""
        servings = float(entry_dict.get("servings", 1.0) or 1.0)

        # Derive ``date`` from ``day_num`` when the trash schema didn't
        # carry an explicit ``date`` field (T4 owns surfacing ``date`` on
        # the FLE; until then we fall back to today).
        when: date | None = None
        if date_str:
            try:
                when = date.fromisoformat(date_str)
            except ValueError:
                when = None

        is_last = line_no == total
        result: dict[str, Any] = {
            "trash_file": str(path),
            "line_no": line_no,
            "total_lines": total,
            "is_last": is_last,
            "food_id": food_id,
            "food_name": food_name,
            "meal": meal_name,
            "date": date_str,
            "servings": servings,
            "logged": None,
            "consumed": False,
            "dry_run": dry_run,
            "keep": keep,
        }

        if dry_run:
            return result

        # Replay through the existing high-level path. We need a
        # ``MealType`` for log_food's parser — ``meal_name`` already
        # comes from ``MealType(n).name`` so it round-trips.
        try:
            meal_type = MealType.parse(meal_name)
        except ValueError:
            meal_type = MealType.snacks

        if not food_id:
            raise ValueError(f"trash record on line {line_no} has no food_id — cannot re-log")

        logged = self.log_food(
            food=food_id,
            meal=meal_type,
            servings=servings,
            when=when,
        )
        result["logged"] = logged

        if not keep:
            # Atomic rewrite: build the new content (omitting the line),
            # write to a tmp file in the same dir, fsync, os.replace.
            remaining = [ln for i, ln in enumerate(lines, start=1) if i != line_no]
            new_text = ("\n".join(remaining) + "\n") if remaining else ""
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, path)
            # Best-effort chmod — the file already existed and the perm
            # bits transferred via the replace on POSIX. Don't fail
            # the whole restore over a chmod hiccup.
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            result["consumed"] = True

        return result

    # ── Bootstrap (login) ───────────────────────────────────────────────

    @classmethod
    def login_from_browser(
        cls,
        browser: str = "chrome",
        *,
        token_file: Path = DEFAULT_TOKEN_FILE,
        config_file: Path = DEFAULT_CONFIG_FILE,
        user_name: str | None = None,
        write_config: bool = True,
        prompt_for_username: Callable[[], str | None] | None = None,
    ) -> LoginResult:
        """Import the ``liauth`` JWT from a browser; optionally write YAML config.

        Composes :func:`lose_it.core.auth.refresh_token_from_browser`
        + :func:`save_token` + :func:`lose_it.core._login_flow.derive_config_values`
        + :func:`lose_it.core._settings.write_yaml_config` so the CLI's ``login``
        command can shrink to a few lines of flag plumbing + a single
        ``LoseIt.login_from_browser(...)`` call.

        Returns a :class:`LoginResult` regardless of success — inspect
        ``.status`` (``"ok"`` / ``"missing"`` / ``"expired"``) to decide
        whether to open the signin URL.

        ``prompt_for_username`` is a callable matching the
        :data:`lose_it.core._login_flow.derive_config_values`
        signature; passing ``None`` means non-interactive (skips
        prompting and returns a partial result when the username
        can't be auto-resolved).
        """
        token = refresh_token_from_browser(browser)  # type: ignore[arg-type]

        if token is None:
            return LoginResult(
                status="missing",
                browser=browser,
                token_file=token_file,
                exp=None,
                exp_iso=None,
                config_file=None,
                config_values=None,
                signin_url=_SIGNIN_URL,
                message=f"No liauth cookie found in {browser.title()} for loseit.com.",
            )

        exp = decode_jwt_exp(token)
        exp_iso = datetime.fromtimestamp(exp, tz=UTC).isoformat() if exp is not None else None

        if is_token_expired(token):
            return LoginResult(
                status="expired",
                browser=browser,
                token_file=token_file,
                exp=exp,
                exp_iso=exp_iso,
                config_file=None,
                config_values=None,
                signin_url=_SIGNIN_URL,
                message=f"liauth cookie in {browser.title()} is expired.",
            )

        save_token(token, token_file)

        config_values_dict: dict[str, object] | None = None
        written_config: Path | None = None
        if write_config:
            derived = derive_config_values(
                token,
                browser_name=browser,
                user_name_override=user_name,
                prompt_for_username=prompt_for_username,
            )
            if derived.user_name is not None:
                config_values_dict = derived.as_yaml_dict()
                written_config = write_yaml_config(config_file, config_values_dict)

        return LoginResult(
            status="ok",
            browser=browser,
            token_file=token_file,
            exp=exp,
            exp_iso=exp_iso,
            config_file=written_config,
            config_values=config_values_dict,
        )
