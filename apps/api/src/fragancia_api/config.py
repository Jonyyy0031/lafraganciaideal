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
    # Secret behind the private order links (orders, decision 31). Never rotate it: rotating
    # breaks every link already sent.
    order_link_secret: SecretStr | None = None
    # Account emails (identity) and SMTP.
    invitation_ttl_hours: int = Field(default=72, ge=1)
    password_reset_ttl_minutes: int = Field(default=60, ge=1)
    admin_web_url: str = "http://localhost:4200"
    # The storefront's base URL: the customer emails link to `{storefront_url}/pedido#token=...`.
    storefront_url: str = "http://localhost:4200"
    smtp_host: str = "127.0.0.1"
    smtp_port: int = Field(default=1026, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    mail_from: str = "La Fragancia Ideal <no-reply@lafraganciaideal.test>"
    # Media storage (catalog photos).
    s3_endpoint_url: str = "http://127.0.0.1:9100"
    s3_access_key: str | None = None
    s3_secret_key: SecretStr | None = None
    s3_bucket: str = "fragancia-media"
    s3_region: str = "us-east-1"
    media_public_base_url: str = "http://127.0.0.1:9100/fragancia-media"
    photo_max_bytes: int = Field(default=10_485_760, ge=1)
    # Catalog import from an .xlsx file (validated by the worker).
    catalog_import_max_bytes: int = Field(default=5_242_880, ge=1)
    catalog_import_max_rows: int = Field(default=2000, ge=1)
    catalog_import_ttl_minutes: int = Field(default=60, ge=1)
    # Only tests and local QA allow image URLs on private or loopback hosts.
    catalog_import_image_allow_private_hosts: bool = False
    # Largest accepted JSON, form or text request body (binary uploads have their own limit).
    request_max_bytes: int = Field(default=1_048_576, ge=1)

    @model_validator(mode="after")
    def _check_order_link_secret(self) -> Self:
        if self.is_production:
            secret = self.order_link_secret
            if secret is None or len(secret.get_secret_value()) < 32:
                raise ValueError("ORDER_LINK_SECRET must have at least 32 characters in production")
        return self

    @model_validator(mode="after")
    def _check_admin_web_url(self) -> Self:
        url = self.admin_web_url
        if not url.startswith(("http://", "https://")) or url.endswith("/"):
            raise ValueError("ADMIN_WEB_URL must start with http:// or https:// and not end with /")
        return self

    @model_validator(mode="after")
    def _check_storefront_url(self) -> Self:
        url = self.storefront_url
        if not url.startswith(("http://", "https://")) or url.endswith("/"):
            raise ValueError(
                "STOREFRONT_URL must start with http:// or https:// and not end with /"
            )
        return self

    @model_validator(mode="after")
    def _check_media_public_base_url(self) -> Self:
        url = self.media_public_base_url
        if not url.startswith(("http://", "https://")) or url.endswith("/"):
            raise ValueError(
                "MEDIA_PUBLIC_BASE_URL must start with http:// or https:// and not end with /"
            )
        return self

    @property
    def upload_max_bytes(self) -> int:
        """The largest binary upload any route accepts (the body limit for binary media types)."""
        return max(self.photo_max_bytes, self.catalog_import_max_bytes)

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")
