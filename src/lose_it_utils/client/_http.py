"""Thin httpx wrapper that posts GWT-RPC envelopes to ``/web/service``.

Provides one method, :meth:`HttpClient.post_rpc`, that handles:

- the constant GWT headers (``content-type``, ``x-gwt-permutation``, etc.),
- attaching the ``liauth`` cookie to every request,
- recognizing GWT-level ``//EX`` error responses and surfacing them as
  exceptions instead of returning a body the caller has to re-check.
"""
from __future__ import annotations

import re

import httpx

from ._config import Config


class LoseItError(RuntimeError):
    """GWT-level error (``//EX[…]``) or unexpected response shape."""


class LoseItAuthError(LoseItError):
    """HTTP 401/403 — token expired or invalid."""


class HttpClient:
    """Owns the httpx session, headers, and cookies for a Lose It! account."""

    def __init__(self, config: Config, token: str, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.token = token
        headers = {
            "content-type": "text/x-gwt-rpc; charset=UTF-8",
            "x-gwt-module-base": config.base_url,
            "x-gwt-permutation": config.strong_name,
            "x-loseit-gwtversion": "devmode",
            "x-loseit-hoursfromgmt": str(config.hours_from_gmt),
            "origin": "https://www.loseit.com",
            "referer": "https://www.loseit.com/",
        }
        cookies = httpx.Cookies()
        cookies.set("liauth", token, domain="www.loseit.com", path="/")
        cookies.set("fn_auth", token, domain="www.loseit.com", path="/")
        self._client = httpx.Client(
            headers=headers, cookies=cookies, timeout=30.0, transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def post_rpc(self, payload: str) -> str:
        """POST a GWT-RPC envelope; return the ``//OK[…]`` response text.

        Raises :class:`LoseItAuthError` on 401/403, :class:`LoseItError` on
        any other non-OK response (including GWT-level ``//EX[…]`` errors).
        """
        resp = self._client.post(self.config.service_url, content=payload)
        if resp.status_code in (401, 403):
            raise LoseItAuthError(f"HTTP {resp.status_code}: token expired or invalid")
        if resp.status_code != 200:
            raise LoseItError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        text = resp.text
        if text.startswith("//EX"):
            match = re.search(r'"([^"]*)"', text)
            raise LoseItError(f"GWT error: {match.group(1) if match else text[:200]}")
        if not text.startswith("//OK"):
            raise LoseItError(f"Unexpected response: {text[:200]}")
        return text
