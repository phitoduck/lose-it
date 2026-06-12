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

from datetime import date
from pathlib import Path
from typing import Any

import httpx

from ._logging import logger
from .core import auth as _auth
from .core._config import Config, MissingConfigError
from .core._http import HttpClient
from .enums import MealType, ServingUnit
from .models import (
    FoodDescription,
    FoodLogEntry,
    FoodSearchResult,
    LoggedFood,
    LoginResult,
    UnsavedFoodLogEntry,
)

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
        raise NotImplementedError

    def close(self) -> None:
        """Close the underlying httpx session. Idempotent."""
        raise NotImplementedError

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
        raise NotImplementedError

    # ── Food lookup ─────────────────────────────────────────────────────

    def search(self, query: str) -> list[FoodSearchResult]:
        """Search the LoseIt food database. Returns up to ~15 results.

        Thin wrapper over :func:`lose_it.core.foods.search`; folded
        here so callers can use ``li.search(...)`` instead of
        ``foods.search(li.http, ...)``.
        """
        raise NotImplementedError

    def get_food(self, food_id: str | list[int]) -> FoodSearchResult:
        """Look up a food by ID (hex string or raw 16-byte list).

        Accepts the lowercase-hex form exposed by ``loseit search``'s
        ``Food ID`` column / JSON ``food_id`` field, or the raw
        ``pk_bytes`` list returned by :meth:`search`. Hex strings get
        validated/decoded via :func:`lose_it.core._ids.hex_to_pk`.
        """
        raise NotImplementedError

    def get_food_template(
        self, food: FoodSearchResult | str | list[int]
    ) -> UnsavedFoodLogEntry:
        """Fetch the unsaved-entry template for a food (nutrient + serving sizes).

        Accepts either a :class:`FoodSearchResult` (skips the ``getFood``
        round-trip) or a food id (in which case ``getFood`` is called
        first to resolve the name/brand the unsaved-entry RPC needs).
        """
        raise NotImplementedError

    def describe_food(self, food_id: str) -> FoodDescription:
        """Return the full nutrient + serving profile for one food.

        Internally: ``get_food`` → ``get_unsaved_food_log_entry`` →
        synthesize a :class:`FoodDescription`. Cheaper than calling
        those two RPCs by hand and remembering which fields to project.
        """
        raise NotImplementedError

    def describe_foods(self, food_ids: list[str]) -> list[FoodDescription]:
        """Describe multiple foods concurrently.

        Each ID is fetched in parallel via ``asyncio.to_thread`` over the
        sync HTTP client — N foods take ~max(per-request-latency) rather
        than sum. Invalid IDs surface as :class:`FoodDescription` rows
        with ``name=""`` and the failure recorded — callers can filter,
        or use :meth:`describe_food` per-ID when they want exceptions.
        """
        raise NotImplementedError

    # ── Diary CRUD ──────────────────────────────────────────────────────

    def diary(self, when: date | None = None) -> list[FoodLogEntry]:
        """List the day's diary entries. ``when=None`` → today."""
        raise NotImplementedError

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
        raise NotImplementedError

    def delete_entry(self, entry: FoodLogEntry) -> None:
        """Delete a diary entry. The whole entry payload is required by the server."""
        raise NotImplementedError

    # ── Bootstrap (login) ───────────────────────────────────────────────

    @classmethod
    def login_from_browser(
        cls,
        browser: str = "chrome",
        *,
        token_file: Path | None = None,
        config_file: Path | None = None,
        user_name: str | None = None,
        write_config: bool = True,
        prompt_for_username: object | None = None,
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
        raise NotImplementedError
