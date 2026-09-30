"""Application configuration.

All runtime configuration comes from environment variables (optionally loaded
from a ``.env`` file).  Nothing here talks to the network or the database, so it
is safe to import from anywhere — including Alembic and the test-suite.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

#: Placeholder used in ``.env.example``; treated as "not set".
_EMPTY_MARKERS = {"", "changeme", "change-me", "your-secret-key"}


def _split_ids(raw: str | None) -> list[int]:
    """Parse a comma/space separated list of Telegram numeric ids."""
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out


class Settings(BaseSettings):
    """Typed, validated application settings."""

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- core ---------------------------------------------------------------
    app_name: str = "فروشگاه VPN"
    env: Literal["production", "development", "test"] = "production"
    debug: bool = False
    timezone: str = "Asia/Tehran"
    log_level: str = "INFO"
    secret_key: str = ""

    # -- telegram -----------------------------------------------------------
    bot_token: str = ""
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""
    webhook_secret: str = ""
    bot_api_server: str = ""
    bot_proxy: str = ""

    # -- database -----------------------------------------------------------
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "wgguard"
    postgres_user: str = "wgguard"
    postgres_password: str = ""
    database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # -- redis --------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # -- panel --------------------------------------------------------------
    panel_enabled: bool = True
    panel_host: str = "0.0.0.0"
    panel_port: int = 8080
    panel_base_url: str = "http://localhost:8080"
    panel_behind_proxy: bool = False
    owner_username: str = "admin"
    owner_password: str = ""
    admin_ids: str = ""
    support_ids: str = ""

    # -- shop ---------------------------------------------------------------
    currency_display: Literal["toman", "rial"] = "toman"
    card_to_card_enabled: bool = True
    wallet_enabled: bool = True
    test_service_enabled: bool = True
    test_service_cooldown_days: int = 30
    receipt_expire_minutes: int = 90
    subscription_base_url: str = ""
    min_deposit_rial: int = 500_000
    referral_percent: float = 0.0

    # -- backups ------------------------------------------------------------
    backup_enabled: bool = True
    backup_interval_hours: int = 24
    backup_keep: int = 7

    # -- advanced -----------------------------------------------------------
    wg_request_timeout: float = 30.0
    wg_connect_timeout: float = 10.0
    wg_max_retries: int = 3
    panel_health_interval: int = 300
    receipt_max_file_mb: int = 8

    # -- derived ------------------------------------------------------------
    @field_validator("currency_display")
    @classmethod
    def _customer_currency(cls, value: str) -> str:
        """Accept legacy Rial settings, but customer copy always uses Toman."""
        return "toman"

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("panel_base_url", "webhook_base_url", "bot_api_server", "subscription_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def _ensure_secrets(self) -> Settings:
        """Generate a development secret so a mis-configured dev box still boots."""
        if self.secret_key in _EMPTY_MARKERS or len(self.secret_key) < 32:
            if self.env == "production":
                # Fail loudly rather than silently signing cookies with a weak key.
                raise ValueError(
                    "SECRET_KEY is missing or too short (need >= 32 chars). "
                    "Run ./install.sh or set it manually: openssl rand -hex 32"
                )
            object.__setattr__(self, "secret_key", secrets.token_hex(32))
        if self.webhook_secret in _EMPTY_MARKERS:
            object.__setattr__(self, "webhook_secret", secrets.token_urlsafe(24))
        return self

    # -- computed -----------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        pwd = quote(self.postgres_password, safe="")
        user = quote(self.postgres_user, safe="")
        return f"postgresql+asyncpg://{user}:{pwd}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_sqlalchemy_url(self) -> str:
        """Sync DSN (psycopg-free) used by Alembic's offline mode only."""
        return self.sqlalchemy_url.replace("+asyncpg", "")

    @property
    def admin_id_list(self) -> list[int]:
        return _split_ids(self.admin_ids)

    @property
    def support_id_list(self) -> list[int]:
        return _split_ids(self.support_ids)

    @property
    def all_staff_ids(self) -> list[int]:
        return sorted({*self.admin_id_list, *self.support_id_list})

    @property
    def webhook_path(self) -> str:
        return f"/tg/webhook/{self.webhook_secret}"

    @property
    def panel_login_url(self) -> str:
        return f"{self.panel_base_url}{self.panel_prefix}/login"

    @property
    def panel_prefix(self) -> str:
        return "/panel"

    def as_public_dict(self) -> dict[str, Any]:
        """Safe-to-display subset (never expose tokens)."""
        return {
            "app_name": self.app_name,
            "env": self.env,
            "debug": self.debug,
            "timezone": self.timezone,
            "bot_mode": self.bot_mode,
            "currency_display": self.currency_display,
            "panel_enabled": self.panel_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


settings = get_settings()
