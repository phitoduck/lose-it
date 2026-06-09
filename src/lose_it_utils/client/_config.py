"""Configuration constants for the Lose It! client.

Two classes of config end up in a :class:`Config`:

1. **Per-account values** (``user_id``, ``user_name``, ``hours_from_gmt``).
   These identify *you* and have **no default** — they must come from env
   vars or explicit kwargs, otherwise ``Config.from_env`` raises. We do not
   ship author-tied defaults because (a) they identify a specific human
   and (b) silently falling back to someone else's account would make
   debugging "I posted to whose diary?" surprises impossible.

2. **Per-build values** (``policy_hash``, ``strong_name``). These are GWT
   compile-time signatures of LoseIt's compiled web client; they change
   every time LoseIt redeploys. The defaults below are *whatever was current
   when this file was last touched*; refresh via the ``LOSEIT_POLICY_HASH``
   and ``LOSEIT_STRONG_NAME`` env vars when requests start returning
   ``IncompatibleRemoteServiceException``.

> **Why aren't the ``Class/<digits>`` strings in this repo a leak?** Numbers
> like ``UserId/4281239478`` or ``ServiceRequestToken/1076571655`` are GWT
> type-serialization hashes derived from each Java class's fields. They are
> identical for every user of the same client build and are inlined in the
> public ``*.cache.js`` bundle anyone can ``curl`` from
> ``d3hsih69yn4d89.cloudfront.net``. They are protocol type tags, not
> credentials or session identifiers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE_URL = "https://www.loseit.com/web/service"
BASE_URL = "https://d3hsih69yn4d89.cloudfront.net/web/"


class MissingConfigError(EnvironmentError):
    """Raised when a required ``LOSEIT_*`` env var is unset."""


@dataclass(frozen=True)
class Config:
    """Per-account + per-build configuration. Build via :meth:`from_env`."""

    user_id: str
    user_name: str
    hours_from_gmt: int
    policy_hash: str
    strong_name: str
    base_url: str = BASE_URL
    service_url: str = SERVICE_URL

    @classmethod
    def from_env(cls, **overrides) -> Config:
        """Build a Config from ``LOSEIT_*`` env vars; kwargs override env.

        Required (no default; raises :class:`MissingConfigError` if absent
        and not provided as a kwarg):

        - ``LOSEIT_USER_ID`` — numeric ``sub`` claim of your ``liauth`` JWT
        - ``LOSEIT_USER_NAME`` — your loseit.com username
        - ``LOSEIT_HOURS_FROM_GMT`` — your local offset from UTC (e.g. ``-5``)

        Optional (have safe defaults that may go stale on redeploy):

        - ``LOSEIT_POLICY_HASH``
        - ``LOSEIT_STRONG_NAME``
        """
        env = os.environ
        required = {
            "user_id": ("LOSEIT_USER_ID", env.get("LOSEIT_USER_ID")),
            "user_name": ("LOSEIT_USER_NAME", env.get("LOSEIT_USER_NAME")),
            "hours_from_gmt": (
                "LOSEIT_HOURS_FROM_GMT",
                env.get("LOSEIT_HOURS_FROM_GMT"),
            ),
        }
        resolved: dict[str, object] = {}
        missing: list[str] = []
        for field, (env_name, env_val) in required.items():
            if field in overrides:
                resolved[field] = overrides.pop(field)
            elif env_val:
                resolved[field] = env_val
            else:
                missing.append(env_name)
        if missing:
            raise MissingConfigError(
                "Missing required env var(s): " + ", ".join(missing) + ". See README for setup."
            )
        # hours_from_gmt arrives as a str (env) or int (kwarg); coerce.
        resolved["hours_from_gmt"] = int(resolved["hours_from_gmt"])  # type: ignore[arg-type]

        defaults = {
            "policy_hash": env.get("LOSEIT_POLICY_HASH", "8F87EC8969F17AE77B6283D3A83F6D4C"),
            "strong_name": env.get("LOSEIT_STRONG_NAME", "351AE5DC0CA36AD3BA9C7CBA7B0E07B8"),
        }
        defaults.update(overrides)
        return cls(**resolved, **defaults)


MEAL_NAMES = {0: "breakfast", 1: "lunch", 2: "dinner", 3: "snacks"}
MEAL_TYPES = {v: k for k, v in MEAL_NAMES.items()} | {"snack": 3}

# Nutrient ordinals → human label. The server only accepts these 9 ordinals
# in the FoodNutrients HashMap when constructing FoodLogEntry payloads.
NUTRIENT_NAMES = {
    0: "calories",
    2: "fat",
    3: "saturated_fat",
    8: "cholesterol",
    9: "sodium",
    10: "carbs",
    11: "fiber",
    12: "sugar",
    13: "protein",
}

# A known day_num/date anchor from the GWT request sniffing session.
# Day numbers are sequential integers; the anchor lets us convert dates ↔ day_num.
DAY_NUM_ANCHOR_DATE = "2026-02-02"
DAY_NUM_ANCHOR_VALUE = 9164
