"""Client configuration — thin façade over :mod:`_settings`.

Historically this file defined a plain ``@dataclass(frozen=True)`` Config plus
a ``from_env`` classmethod. As part of the 12-factor refactor the underlying
loader moved to :mod:`_settings` (pydantic-settings) which supports a layered
priority — CLI > env > YAML > defaults — using the same field set as the
public spec of the YAML file.

For continuity, ``Config`` is now an alias for :class:`Settings`. The
``Config.from_env`` classmethod is preserved so existing call-sites and
fixtures keep working. Direct construction is also still supported (the
pydantic source layering means any unset field will be filled from
env/YAML/defaults — pass ``Config.model_construct(...)`` in tests where
you need to bypass env loading entirely).

The two classes of config (per-account vs per-build) are documented on
the :class:`Settings` class itself.
"""

from __future__ import annotations

from typing import Any

from ._settings import (
    BASE_URL,
    DEFAULT_CONFIG_FILE,
    DEFAULT_TOKEN_FILE,
    SERVICE_URL,
    MissingConfigError,
    Settings,
    load_settings,
)

__all__ = [
    "BASE_URL",
    "DAY_NUM_ANCHOR_DATE",
    "DAY_NUM_ANCHOR_VALUE",
    "DEFAULT_CONFIG_FILE",
    "DEFAULT_TOKEN_FILE",
    "MEAL_NAMES",
    "MEAL_TYPES",
    "NUTRIENT_NAMES",
    "SERVICE_URL",
    "Config",
    "MissingConfigError",
]


class Config(Settings):
    """Per-account + per-build configuration.

    Concrete subclass of :class:`Settings` kept for backwards compatibility
    with existing call-sites that import ``Config`` directly.
    """

    @classmethod
    def from_env(cls, **overrides: Any) -> Config:
        """Build a Config from the layered sources, with kwargs as CLI overrides.

        Identical to ``load_settings(**overrides)`` but returns a ``Config``
        instance for type-narrowing. Layered priority (highest first):
        kwargs > env vars > YAML file > field defaults.

        Raises :class:`MissingConfigError` if any of ``user_id`` /
        ``user_name`` / ``hours_from_gmt`` is unset across all sources.
        """
        # load_settings already drops None overrides; just re-tag the type.
        settings = load_settings(**overrides)
        return cls.model_construct(**settings.model_dump())


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
