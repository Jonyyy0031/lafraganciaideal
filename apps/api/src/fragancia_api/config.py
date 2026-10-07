"""Typed settings from the environment and `apps/api/.env` (see `apps/api/.env.example`).

Invalid or missing settings fail at startup with a clear message, never at first use.
"""

from functools import cached_property
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

API_ROOT = Path(__file__).resolve().parents[2]

type AppEnv = Literal["development", "test", "production"]


class DatabaseSettings(BaseSettings):
    """Only what migrations need (the `migrator` image has no Valkey settings)."""

    model_config = SettingsConfigDict(env_file=API_ROOT / ".env", extra="ignore", frozen=True)

    app_env: AppEnv = "development"
    database_url: str
    database_url_test: str | None = None

    @model_validator(mode="after")
    def _check_databases(self) -> Self:
        for name in ("database_url", "database_url_test"):
            url = getattr(self, name)
            if url is not None and not url.startswith("postgresql+psycopg://"):
                raise ValueError(f"{name.upper()} must use the postgresql+psycopg:// driver")
        if self.database_url_test is not None and not _database_name(
            self.database_url_test
        ).endswith("_test"):
            raise ValueError("DATABASE_URL_TEST must point to a database whose name ends in _test")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def for_tests(self) -> Self:
        """The same settings pointing at the test database (integration tests only)."""
        if self.database_url_test is None:
            raise ValueError("DATABASE_URL_TEST is required for integration tests")
        return self.model_copy(update={"app_env": "test", "database_url": self.database_url_test})


class Settings(DatabaseSettings):
    api_port: int = 8100
    valkey_url: str
    cors_origins: str = ""
    log_level: str = "INFO"
    # Back-office sessions and login throttle (identity module).
    session_idle_minutes: int = Field(default=120, ge=1)
    session_max_hours: int = Field(default=12, ge=1)
    login_email_max_attempts: int = Field(default=5, ge=1)
    login_ip_max_attempts: int = Field(default=50, ge=1)
    login_window_minutes: int = Field(default=15, ge=1)
    # Account emails (identity) and SMTP.
    invitation_ttl_hours: int = Field(default=72, ge=1)
    password_reset_ttl_minutes: int = Field(default=60, ge=1)
    admin_web_url: str = "http://localhost:4200"
    smtp_host: str = "127.0.0.1"
    smtp_port: int = Field(default=1026, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    mail_from: str = "La Fragancia Ideal <no-reply@lafraganciaideal.test>"

    @model_validator(mode="after")
    def _check_admin_web_url(self) -> Self:
        url = self.admin_web_url
        if not url.startswith(("http://", "https://")) or url.endswith("/"):
            raise ValueError("ADMIN_WEB_URL must start with http:// or https:// and not end with /")
        return self

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")
