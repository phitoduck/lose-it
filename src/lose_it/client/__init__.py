"""Lose It! client SDK.

The :class:`Client` holds account configuration + the httpx session. All
RPC functions live in submodules and accept a ``Client`` as their first
argument, so the API surface looks like::

    from lose_it import Client
    from lose_it.client import foods, entries, daily

    with Client.from_env() as c:
        results = foods.search(c.http, "tortilla")
        unsaved = foods.get_unsaved_food_log_entry(c.http, results[0])
        entries.log_food(c.http, unsaved, meal_ordinal=1,
                         day_key=..., day_num=..., servings=1.0)
        for e in daily.get_daily_details(c.http, today):
            print(e.food_name, e.servings)
"""

from __future__ import annotations

import httpx

from .._logging import logger
from . import auth as _auth
from ._config import Config, MissingConfigError
from ._http import HttpClient, LoseItAuthError, LoseItError


class Client:
    """Top-level handle: account config + authenticated httpx session."""

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
        **config_overrides,
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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


__all__ = [
    "Client",
    "Config",
    "HttpClient",
    "LoseItAuthError",
    "LoseItError",
    "MissingConfigError",
]
