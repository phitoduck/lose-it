"""Unofficial Lose It! Python SDK and CLI.

Reverse-engineered GWT-RPC client for loseit.com. Provides:

- A ``Client`` that owns HTTP state (httpx) and account configuration.
- Domain modules under ``lose_it_utils.client``: ``foods``, ``entries``, ``daily``,
  ``init``, ``auth``. Each module mirrors a LoseIt backend resource and exposes
  one function per RPC method.
- A CLI (``lose-it-utils.cli``) covering search, log, list, delete, replay.
"""

from .client import Client

__all__ = ["Client"]
