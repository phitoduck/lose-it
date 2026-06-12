"""Unofficial Lose It! Python SDK and CLI.

Reverse-engineered GWT-RPC client for loseit.com. Provides:

- :class:`LoseIt` — high-level client. One method per user-facing
  capability; composes pure helpers + low-level RPCs. Start here.
- :class:`Client` — low-level handle (owns HTTP state + Config). Used by
  the module-level RPC functions in ``lose_it.client.{foods, entries,
  daily, init, auth}``. Reach for it when you need direct control over
  a specific RPC.
- A CLI (``loseit``, implemented in ``lose_it.cli``) covering search,
  log, diary, delete, describe-food, login, whoami — itself a thin
  wrapper over :class:`LoseIt`.
"""

from .client import Client, LoseIt

__all__ = ["Client", "LoseIt"]
