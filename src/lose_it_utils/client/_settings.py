"""Layered configuration for the Lose It! client (12-factor style).

All settings can be supplied from any of four sources. Higher-priority
sources override lower-priority ones field-by-field:

1. **CLI flags / explicit init kwargs** (highest priority)
2. **Environment variables** (``LOSEIT_*``)
3. **YAML file** (default: ``~/.config/loseit/config.yaml``)
4. **Built-in field defaults** (lowest priority)

The YAML file's schema is the :class:`Settings` model itself — every field
documented in :class:`Settings` is a valid YAML key. A missing YAML file is
not an error; it just means the YAML layer contributes nothing. A missing
required field (``user_id`` / ``user_name`` / ``hours_from_gmt``) after all
layers are merged raises :class:`MissingConfigError`.

The class is :mod:`pydantic-settings`-backed so the layering, env-var
parsing, and YAML loading are all handled by upstream code we don't have
to maintain.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

SERVICE_URL = "https://www.loseit.com/web/service"
BASE_URL = "https://d3hsih69yn4d89.cloudfront.net/web/"

DEFAULT_CONFIG_FILE = Path("~/.config/loseit/config.yaml").expanduser()
DEFAULT_TOKEN_FILE = Path("~/.config/loseit/token").expanduser()


class MissingConfigError(EnvironmentError):
    """Raised when a required ``LOSEIT_*`` setting is unset in every source."""


class Settings(BaseSettings):
    """Layered Lose It! client configuration.

    Source priority (highest wins):

    1. Init kwargs (passed by the CLI from ``--user-id`` etc.)
    2. ``LOSEIT_*`` environment variables
    3. YAML file (path resolved from ``--config-file`` / ``LOSEIT_CONFIG_FILE``
       / default ``~/.config/loseit/config.yaml``)
    4. Field defaults
    """

    model_config = SettingsConfigDict(
        env_prefix="LOSEIT_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        yaml_file=str(DEFAULT_CONFIG_FILE),
    )

    # ── Account identifiers (required; raise if absent in every source) ──
    user_id: str | None = None
    user_name: str | None = None
    hours_from_gmt: int | None = None

    # ── Per-build (refreshed when LoseIt redeploys) ──
    policy_hash: str = "8F87EC8969F17AE77B6283D3A83F6D4C"
    strong_name: str = "351AE5DC0CA36AD3BA9C7CBA7B0E07B8"

    # ── URLs (rarely overridden; exposed for testing) ──
    base_url: str = BASE_URL
    service_url: str = SERVICE_URL

    # ── Auth & config-file paths ──
    token: str | None = None
    token_file: Path = DEFAULT_TOKEN_FILE
    config_file: Path = DEFAULT_CONFIG_FILE

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Resolve which YAML file to read up-front, since the YAML source
        # itself can't read its own config_file field. CLI/init > env > default.
        init_kwargs: dict[str, Any] = getattr(init_settings, "init_kwargs", {}) or {}
        yaml_file = (
            init_kwargs.get("config_file")
            or os.environ.get("LOSEIT_CONFIG_FILE")
            or DEFAULT_CONFIG_FILE
        )
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file)
        # First source wins.
        return (init_settings, env_settings, yaml_source)

    @model_validator(mode="after")
    def _require_account_identifiers(self) -> Settings:
        missing = [
            name
            for name in ("user_id", "user_name", "hours_from_gmt")
            if getattr(self, name) is None
        ]
        if missing:
            env_vars = ", ".join(f"LOSEIT_{n.upper()}" for n in missing)
            raise MissingConfigError(
                "Missing required setting(s): "
                + ", ".join(missing)
                + f". Set via {env_vars}, YAML file, or CLI flags. See README."
            )
        return self


def load_settings(**overrides: Any) -> Settings:
    """Construct :class:`Settings` from the layered sources.

    ``overrides`` are treated as init kwargs (highest-priority CLI layer).
    ``None`` values are dropped so they don't shadow lower-priority sources.
    Pydantic ``ValidationError`` raised by the required-field check is
    re-raised as :class:`MissingConfigError` for a friendlier message.
    """
    kwargs = {k: v for k, v in overrides.items() if v is not None}
    try:
        return Settings(**kwargs)
    except ValidationError as e:
        for err in e.errors():
            ctx = err.get("ctx") or {}
            inner = ctx.get("error")
            if isinstance(inner, MissingConfigError):
                raise inner from None
        raise
