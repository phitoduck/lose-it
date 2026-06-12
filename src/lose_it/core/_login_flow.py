"""Pure helpers for the ``login`` bootstrap (token → YAML config values).

The ``login`` command does two things: import the ``liauth`` JWT from a
browser cookie store, and populate the YAML config so subsequent CLI
invocations don't need ``LOSEIT_*`` env vars.

The second half is mostly pure: given a JWT (and optionally the
browser's other cookies, and optionally an interactive prompt), derive
``user_id`` / ``user_name`` / ``hours_from_gmt``. Decoupling it from the
CLI lets the high-level :class:`~lose_it.LoseIt` client invoke the same
flow without dragging in ``typer``.

Lifted from the ``_detect_hours_from_gmt`` and
``_populate_config_from_login`` helpers in the old ``cli.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .auth import (
    extract_user_info_from_jwt,
    extract_user_name_from_cookies,
    load_cookies_from_browser,
)

__all__ = ["DerivedConfigValues", "detect_hours_from_gmt", "derive_config_values"]


@dataclass(frozen=True)
class DerivedConfigValues:
    """Config fields resolved from a JWT (+ cookie sniff, + optional prompt).

    Only fields that resolve are populated; the caller decides whether to
    merge with existing YAML or skip the write. ``hours_from_gmt`` always
    resolves (via the OS timezone) so it's a plain int. ``user_id`` /
    ``user_name`` may be ``None`` when the JWT lacks the relevant claim
    and the caller didn't pass a prompt.
    """

    user_name: str | None
    user_id: str | None
    hours_from_gmt: int

    def as_yaml_dict(self) -> dict[str, object]:
        """Project to the dict shape expected by ``write_yaml_config``.

        Drops ``None`` values so a partial resolve doesn't blow away an
        existing field in the YAML file.
        """
        out: dict[str, object] = {"hours_from_gmt": self.hours_from_gmt}
        if self.user_name is not None:
            out["user_name"] = self.user_name
        if self.user_id is not None:
            out["user_id"] = self.user_id
        return out


def detect_hours_from_gmt() -> int:
    """Return the current local UTC offset in whole hours (DST-aware).

    Half-hour zones (India, parts of Australia) round to the nearest
    whole hour — the LoseIt protocol's ``hours_from_gmt`` field is a
    plain integer, so any user in a non-whole-hour zone has to pick
    one side anyway.
    """
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    # round() so ±:30 zones land on whichever hour they're closer to.
    return round(offset.total_seconds() / 3600)


def derive_config_values(
    token: str,
    browser_name: str,
    *,
    user_name_override: str | None = None,
    prompt_for_username: Callable[[], str | None] | None = None,
) -> DerivedConfigValues:
    """Resolve user_id / user_name / hours_from_gmt from a JWT + browser state.

    Priority for ``user_name``:

    1. ``user_name_override`` (explicit ``--user-name`` flag).
    2. JWT payload claim — tries ``email`` / ``preferred_username`` /
       ``username`` / ``name`` / etc. (see ``_USERNAME_CLAIMS`` in auth.py).
    3. The browser's other ``loseit.com`` cookies — scanned for cookie
       names that historically held the signed-in email.
    4. ``prompt_for_username()`` if provided. The callback is the only
       interactive escape hatch; it returns ``None`` to skip.

    ``user_id`` comes from the JWT's ``sub`` claim (or one of the
    documented aliases). ``hours_from_gmt`` always falls back to
    :func:`detect_hours_from_gmt`.

    Returns a :class:`DerivedConfigValues` regardless of completeness;
    inspect ``.user_name`` / ``.user_id`` for ``None`` to decide whether
    the values are safe to write to YAML.
    """
    info = extract_user_info_from_jwt(token)

    user_name: str | None = user_name_override or info.get("user_name")
    if not user_name:
        # browser_name is treated narrowly here; auth.load_cookies_from_browser
        # validates against its Literal type and returns {} if browser-cookie3
        # isn't importable.
        cookies = load_cookies_from_browser(browser_name)  # type: ignore[arg-type]
        user_name = extract_user_name_from_cookies(cookies)
    if not user_name and prompt_for_username is not None:
        user_name = prompt_for_username()
    if user_name is not None:
        user_name = user_name.strip() or None

    return DerivedConfigValues(
        user_name=user_name,
        user_id=info.get("user_id"),
        hours_from_gmt=detect_hours_from_gmt(),
    )
