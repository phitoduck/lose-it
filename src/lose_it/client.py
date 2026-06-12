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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable

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
from .core.init import get_daydate_key
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

# Lose It's signin URL — surfaced in LoginResult for the CLI to display.
_SIGNIN_URL = "https://www.loseit.com/"

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

    def get_food_template(
        self, food: FoodSearchResult | str | list[int]
    ) -> UnsavedFoodLogEntry:
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

    def delete_entry(self, entry: FoodLogEntry) -> None:
        """Delete a diary entry. The whole entry payload is required by the server."""
        _entries.delete(self.http, entry)

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
        exp_iso = (
            datetime.fromtimestamp(exp, tz=UTC).isoformat() if exp is not None else None
        )

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
