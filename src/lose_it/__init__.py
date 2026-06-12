"""Unofficial Lose It! Python SDK and CLI.

Reverse-engineered GWT-RPC client for loseit.com. Provides:

- :class:`LoseIt` — high-level client. One method per user-facing
  capability; composes pure helpers + low-level RPCs. Start here.
- :class:`Client` — low-level handle (owns HTTP state + Config). Used by
  the module-level RPC functions in ``lose_it.core.{foods, entries,
  daily, init, auth}``. Reach for it when you need direct control over
  a specific RPC.
- A CLI (``loseit``, implemented in :mod:`lose_it.cli`) covering search,
  log, diary, delete, describe-food, login, whoami — itself a thin
  wrapper over :class:`LoseIt`.

The return types from :class:`LoseIt`'s methods are exported here too so
typical SDK use needs only one import line::

    from lose_it import LoseIt, FoodSearchResult, LoggedFood
"""

from .client import Client, LoseIt
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

__all__ = [
    "Client",
    "CrossClassConversion",
    "FoodDescription",
    "FoodLogEntry",
    "FoodSearchResult",
    "LoggedFood",
    "LoginResult",
    "LoseIt",
    "PrimaryServing",
    "UnsavedFoodLogEntry",
]
