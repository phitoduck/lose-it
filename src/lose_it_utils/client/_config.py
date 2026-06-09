"""Configuration constants for the Lose It! client.

The GWT signatures (``POLICY_HASH`` / ``STRONG_NAME``) change whenever LoseIt
redeploys their web app — these are the latest values observed during
development and are expected to be overridden by env vars in production use.
Defaults are kept here so tests can run without the env being set up.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


SERVICE_URL = "https://www.loseit.com/web/service"
BASE_URL = "https://d3hsih69yn4d89.cloudfront.net/web/"


@dataclass(frozen=True)
class Config:
    """Per-account + per-build configuration. Override fields via env vars."""

    user_id: str
    user_name: str
    hours_from_gmt: int
    policy_hash: str
    strong_name: str
    base_url: str = BASE_URL
    service_url: str = SERVICE_URL

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build a Config from LOSEIT_* env vars, with kwargs as final overrides."""
        env = os.environ
        defaults = {
            "user_id": env.get("LOSEIT_USER_ID", "47596378"),
            "user_name": env.get("LOSEIT_USER_NAME", "Rich"),
            "hours_from_gmt": int(env.get("LOSEIT_HOURS_FROM_GMT", "-5")),
            "policy_hash": env.get("LOSEIT_POLICY_HASH", "5ED2771F63B26294E45551B2D697E7B0"),
            "strong_name": env.get("LOSEIT_STRONG_NAME", "24BBC590737D4E7508A96609A56E11F3"),
        }
        defaults.update(overrides)
        return cls(**defaults)


MEAL_NAMES = {0: "breakfast", 1: "lunch", 2: "dinner", 3: "snacks"}
MEAL_TYPES = {v: k for k, v in MEAL_NAMES.items()} | {"snack": 3}

# Nutrient ordinals → human label. The server only accepts these 9 ordinals
# in the FoodNutrients HashMap when constructing FoodLogEntry payloads.
NUTRIENT_NAMES = {
    0: "calories", 2: "fat", 3: "saturated_fat", 8: "cholesterol",
    9: "sodium", 10: "carbs", 11: "fiber", 12: "sugar", 13: "protein",
}

# A known day_num/date anchor from the GWT request sniffing session.
# Day numbers are sequential integers; the anchor lets us convert dates ↔ day_num.
DAY_NUM_ANCHOR_DATE = "2026-02-02"
DAY_NUM_ANCHOR_VALUE = 9164
