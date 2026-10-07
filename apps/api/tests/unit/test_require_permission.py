from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import admin_router, require_permission
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import ADMIN_HEADERS, assert_admin_routes_are_protected

PERMISSION = "things:manage"

gated = admin_router(prefix="/things", dependencies=[Depends(require_permission(PERMISSION))])


@gated.get("")
async def list_things() -> dict[str, str]:
    return {"ok": "yes"}


class Resolver:
    def __init__(self, permissions: frozenset[str]) -> None:
        self._permissions = permissions

    async def resolve(self, token: str) -> Actor | None:
        if token != "test-admin-session":
            return None
        return Actor(
            id="someone", is_admin=True, session_id=UUID(int=1), permissions=self._permissions
        )


def _app(permissions: frozenset[str]) -> FastAPI:
    services = ServiceRegistry()
    services.add(ActorResolver, Resolver(permissions))  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    return build_app(services, (gated,))


async def _get(app: FastAPI, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/admin/things", headers=headers)
    return response.status_code, response.json()


async def test_an_actor_with_the_permission_gets_through() -> None:
    assert await _get(_app(frozenset({PERMISSION})), ADMIN_HEADERS) == (200, {"ok": "yes"})


@pytest.mark.parametrize("held", [frozenset(), frozenset({"other:manage"})])
async def test_an_actor_without_the_permission_is_403_forbidden(held: frozenset[str]) -> None:
    status, body = await _get(_app(held), ADMIN_HEADERS)

    assert (status, body["code"]) == (403, "FORBIDDEN")


async def test_the_permission_check_comes_after_authentication() -> None:
    status, body = await _get(_app(frozenset({PERMISSION})), {})

    assert (status, body["code"]) == (401, "AUTHENTICATION_REQUIRED")


async def test_the_gated_route_is_still_declared_as_protected_and_documents_403() -> None:
    app = _app(frozenset())

    assert_admin_routes_are_protected(app)
    operation = app.openapi()["paths"]["/api/v1/admin/things"]["get"]
    assert "403" in operation["responses"]
