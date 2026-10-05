"""The HTTP foundation: error shapes, declared access, health, docs and request ids."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from fragancia_api.container import build_container
from fragancia_api.main.http import build_app, create_app
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import admin_router, public_router, require_admin, unwrap
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.kernel import (
    BusinessRuleViolationError,
    ConflictError,
    Err,
    InvalidValueError,
    NotFoundError,
)
from tests.support import (
    ADMIN_HEADERS,
    TestActorResolver,
    admin_operations,
    assert_admin_routes_are_protected,
    make_settings,
)


class EchoBody(BaseModel):
    name: str = Field(min_length=2)


def _routers() -> list[APIRouter]:
    public = public_router(prefix="/probe")

    @public.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @public.get("/error/{kind}")
    async def domain_error(kind: str) -> None:
        errors = {
            "not-found": NotFoundError("X_NOT_FOUND", "missing"),
            "conflict": ConflictError("X_TAKEN", "taken", {"field": "name"}),
            "invalid": InvalidValueError("X_INVALID", "invalid"),
            "rule": BusinessRuleViolationError("X_RULE", "not allowed"),
        }
        unwrap(Err(errors[kind]))

    @public.post("/echo")
    async def echo(body: EchoBody) -> EchoBody:
        return body

    admin = admin_router(prefix="/probe")

    @admin.get("/whoami")
    async def whoami(actor: Annotated[Actor, Depends(require_admin)]) -> dict[str, str]:
        return {"id": actor.id}

    return [public, admin]


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("down")


@dataclass
class CustomerResolver:
    async def resolve(self, token: str) -> Actor | None:
        return Actor(id="customer", is_admin=False) if token == "customer" else None


def make_app(
    *, checks: HealthChecks | None = None, resolver: ActorResolver | None = None, docs: bool = True
) -> FastAPI:
    services = ServiceRegistry()
    services.add(ActorResolver, resolver or TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, checks or HealthChecks(database=_ok, valkey=_ok))
    return build_app(services, _routers(), docs=docs)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=make_app()), base_url="http://t") as c:
        yield c


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


async def test_readiness_reports_each_check(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "valkey": "ok"}}


async def test_readiness_is_503_naming_the_failing_check() -> None:
    app = make_app(checks=HealthChecks(database=_ok, valkey=_down))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "valkey": "error"},
    }


async def test_unknown_route_uses_the_error_shape(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json() == {"code": "NOT_FOUND", "message": "Not Found"}


async def test_unexpected_errors_are_500_without_details_and_with_a_request_id(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/probe/boom")
    assert response.status_code == 500
    assert response.json() == {"code": "INTERNAL_ERROR", "message": "Unexpected error"}
    assert "secret" not in response.text
    assert response.headers["X-Request-ID"]


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("not-found", 404, "X_NOT_FOUND"),
        ("conflict", 409, "X_TAKEN"),
        ("invalid", 422, "X_INVALID"),
        ("rule", 422, "X_RULE"),
    ],
)
async def test_domain_errors_map_by_category(
    client: AsyncClient, kind: str, status: int, code: str
) -> None:
    response = await client.get(f"/api/v1/probe/error/{kind}")
    assert response.status_code == status
    assert response.json()["code"] == code


async def test_domain_error_details_are_returned(client: AsyncClient) -> None:
    response = await client.get("/api/v1/probe/error/conflict")
    assert response.json() == {"code": "X_TAKEN", "message": "taken", "details": {"field": "name"}}


async def test_invalid_input_is_422_with_field_issues(client: AsyncClient) -> None:
    response = await client.post("/api/v1/probe/echo", json={"name": "x"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"]["issues"][0]["location"] == ["body", "name"]


async def test_admin_route_without_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/probe/whoami")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert "WWW-Authenticate" not in response.headers


async def test_admin_route_with_unknown_token_is_401(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/admin/probe/whoami", headers={"Cookie": "fragancia_session=wrong"}
    )
    assert response.status_code == 401


async def test_admin_route_with_admin_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/probe/whoami", headers=ADMIN_HEADERS)
    assert (response.status_code, response.json()) == (200, {"id": "test-admin"})


async def test_admin_route_for_a_non_admin_is_403() -> None:
    app = make_app(resolver=CustomerResolver())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get(
            "/api/v1/admin/probe/whoami", headers={"Cookie": "fragancia_session=customer"}
        )
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


def test_every_admin_route_declares_admin_access() -> None:
    app = make_app()
    assert admin_operations(app) == [("GET", "/api/v1/admin/probe/whoami")]
    assert_admin_routes_are_protected(app)


async def test_docs_are_served_outside_production(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/docs")).status_code == 200
    assert (await client.get("/api/v1/openapi.json")).status_code == 200


async def test_production_app_hides_the_docs() -> None:
    container = build_container(make_settings(app_env="production"))
    app = create_app(container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/v1/docs")).status_code == 404
        assert (await c.get("/api/v1/openapi.json")).status_code == 404
    await container.close()
