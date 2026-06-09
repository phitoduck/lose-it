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
    "DEFAULT_SERVING_SIZE_GRAMS",
    "DEFAULT_TOKEN_FILE",
    "GRAMS_MEASURE_ORDINAL",
    "MEAL_NAMES",
    "MEAL_TYPES",
    "MEASURE_NAMES",
    "NUTRIENT_NAMES",
    "SERVICE_URL",
    "Config",
    "MissingConfigError",
    "measure_name",
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

# FoodMeasurement ordinal → human-readable unit. Empirically verified by
# logging entries and inspecting the official Lose It! web/mobile UI:
# - 8  is rendered as "grams" — the LoseIt food-data convention for ord=8
#   entries is that the per-serving nutrient values are PER 100 g (matches
#   the per-100g convention printed on nutrition labels), and the FoodServing-
#   Size.quantity field is the literal gram count of the consumed portion.
# - 5  is rendered as "each" — used for whole-item entries (one avocado, …).
# - 27 is rendered as "serving" — generic Lose-It-defined "1 serving" of the
#   food (one tortilla wrap, one slice of bread, etc.).
#
# Many other ordinals exist (cup/tbsp/oz/…) but aren't load-bearing for the
# CLI behavior today; we surface them as "(measure {ord})" in user-facing
# output so they're at least disambiguated.
GRAMS_MEASURE_ORDINAL = 8
DEFAULT_SERVING_SIZE_GRAMS = 100.0
MEASURE_NAMES = {5: "each", 8: "grams", 27: "serving"}


def measure_name(ordinal: int | None) -> str:
    """Return a human-readable unit name for a ``FoodMeasurement`` ordinal."""
    if ordinal is None:
        return "serving"
    return MEASURE_NAMES.get(ordinal, f"(measure {ordinal})")


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
