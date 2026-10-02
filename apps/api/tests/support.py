"""Helpers shared by API tests."""

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from fastapi import FastAPI

from fragancia_api.config import Settings
from fragancia_api.shared.kernel import DomainEvent

ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://u:p@127.0.0.1:5433/fragancia",
        "database_url_test": "postgresql+psycopg://u:p@127.0.0.1:5433/fragancia_test",
        "valkey_url": "redis://127.0.0.1:6380/0",
        "admin_dev_token": ADMIN_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@dataclass(frozen=True, kw_only=True)
class SomethingHappened(DomainEvent):
    name: ClassVar[str] = "tests.something.happened"
    thing_id: UUID
    label: str


def admin_operations(app: FastAPI) -> list[tuple[str, str]]:
    """(METHOD, path) of every operation under /api/v1/admin in the OpenAPI document."""
    paths: dict[str, dict[str, object]] = app.openapi()["paths"]
    return [
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith("/api/v1/admin")
        for method in operations
    ]


def assert_admin_routes_are_protected(app: FastAPI) -> None:
    """Declared access: every admin operation requires the bearer scheme."""
    paths: dict[str, dict[str, dict[str, object]]] = app.openapi()["paths"]
    unprotected = [
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        if path.startswith("/api/v1/admin")
        for method, operation in operations.items()
        if {"HTTPBearer": []} not in operation.get("security", [])  # type: ignore[operator]
    ]
    assert unprotected == [], f"admin routes without require_admin: {unprotected}"
