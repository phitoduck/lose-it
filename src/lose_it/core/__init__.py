"""Internal plumbing for :class:`~lose_it.LoseIt`.

Everything in this package is considered "off-road" — it works, it's
documented, but the stability target is the high-level :class:`LoseIt`
client (``lose_it/client.py``). Reach into ``lose_it.core`` directly
when you want a single RPC without the orchestration layer (fixture
capture, debugging, reverse-engineering a new endpoint) and accept
that signatures here may shift between releases.

Two flavors of module live here:

- Underscore-prefixed (``_config``, ``_http``, ``_decoder``, …) — pure
  plumbing the high-level client composes. Implementation detail.
- Unprefixed RPC modules (``foods``, ``entries``, ``daily``, ``init``,
  ``auth``) — one function per LoseIt backend method, each taking an
  :class:`HttpClient` as its first argument. The low-level surface the
  pre-LoseIt SDK exposed.

Public return types live in :mod:`lose_it.models`, not here.
"""

from __future__ import annotations
