"""Lose It! client SDK.

The :class:`Client` holds account configuration + the httpx session. All
RPC functions live in submodules and accept a ``Client`` as their first
argument, so the API surface looks like::

    from lose_it_utils import Client
    from lose_it_utils.client import foods, entries, daily

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

from . import auth as _auth
from ._config import Config
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
        """Build a Client from ``LOSEIT_*`` env vars + the token file."""
        config = Config.from_env(**config_overrides)
        if token is None:
            token = _auth.load_token()
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
]
