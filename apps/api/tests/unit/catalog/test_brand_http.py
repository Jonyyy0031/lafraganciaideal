from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.http.router import routers
from fragancia_api.modules.catalog.infrastructure.in_memory import InMemoryBrands
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.dev_token_actor_resolver import DevTokenActorResolver
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from tests.support import ADMIN_HEADERS, ADMIN_TOKEN, assert_admin_routes_are_protected

BRANDS = "/api/v1/brands"
ADMIN_BRANDS = "/api/v1/admin/brands"


@pytest.fixture
def store() -> InMemoryBrands:
    return InMemoryBrands()


@pytest.fixture
async def client(store: InMemoryBrands) -> AsyncIterator[AsyncClient]:
    services = ServiceRegistry()
    services.add(ActorResolver, DevTokenActorResolver(ADMIN_TOKEN))  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        CreateBrand,
        CreateBrand(
            brands=store,
            transactions=InMemoryTransactionRunner(),
            events=RecordingEventPublisher(),
            clock=FixedClock(),
        ),
    )
    services.add(ListPublicBrands, ListPublicBrands(store))
    services.add(ListAdminBrands, ListAdminBrands(store))
    app = build_app(services, routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_create_brand_returns_201_with_the_id(
    client: AsyncClient, store: InMemoryBrands
) -> None:
    response = await client.post(
        ADMIN_BRANDS, json={"name": " Maison  Margiela"}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 201
    [brand] = store.by_id.values()
    assert response.json() == {"id": str(brand.id)}


async def test_create_brand_requires_an_admin(client: AsyncClient) -> None:
    no_token = await client.post(ADMIN_BRANDS, json={"name": "Dior"})
    wrong_token = await client.post(
        ADMIN_BRANDS, json={"name": "Dior"}, headers={"Authorization": "Bearer nope"}
    )
    assert (no_token.status_code, no_token.json()["code"]) == (401, "AUTHENTICATION_REQUIRED")
    assert wrong_token.status_code == 401


async def test_duplicate_brand_is_409(client: AsyncClient) -> None:
    await client.post(ADMIN_BRANDS, json={"name": "Maison Margiela"}, headers=ADMIN_HEADERS)
    response = await client.post(
        ADMIN_BRANDS, json={"name": "maison margiéla"}, headers=ADMIN_HEADERS
    )
    assert (response.status_code, response.json()["code"]) == (409, "CATALOG_BRAND_ALREADY_EXISTS")


async def test_invalid_brand_name_is_422_with_the_domain_code(client: AsyncClient) -> None:
    response = await client.post(ADMIN_BRANDS, json={"name": "x"}, headers=ADMIN_HEADERS)
    assert response.status_code == 422
    assert response.json() == {
        "code": "CATALOG_BRAND_NAME_INVALID",
        "message": "A brand name needs 2 to 80 characters, including letters or digits",
        "details": {"min": 2, "max": 80},
    }


async def test_missing_name_is_a_validation_error(client: AsyncClient) -> None:
    response = await client.post(ADMIN_BRANDS, json={}, headers=ADMIN_HEADERS)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_public_list_shows_active_brands_without_admin_fields(
    client: AsyncClient, store: InMemoryBrands
) -> None:
    for name in ("Zara", "armani"):
        await client.post(ADMIN_BRANDS, json={"name": name}, headers=ADMIN_HEADERS)
    next(b for b in store.by_id.values() if b.slug == "zara").is_active = False

    response = await client.get(BRANDS)

    assert response.status_code == 200
    [brand] = response.json()
    assert set(brand) == {"id", "name", "slug"}
    assert (brand["name"], brand["slug"]) == ("armani", "armani")


async def test_admin_list_is_paginated(client: AsyncClient) -> None:
    for name in ("Chanel", "Armani", "Boss"):
        await client.post(ADMIN_BRANDS, json={"name": name}, headers=ADMIN_HEADERS)

    response = await client.get(ADMIN_BRANDS, params={"page": 1, "size": 2}, headers=ADMIN_HEADERS)

    body = response.json()
    assert response.status_code == 200
    assert (body["total"], body["page"], body["size"]) == (3, 1, 2)
    assert [b["name"] for b in body["items"]] == ["Armani", "Boss"]
    assert set(body["items"][0]) == {"id", "name", "slug", "is_active", "created_at"}


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}])
async def test_admin_list_rejects_bad_pagination(
    client: AsyncClient, params: dict[str, int]
) -> None:
    response = await client.get(ADMIN_BRANDS, params=params, headers=ADMIN_HEADERS)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_admin_list_requires_an_admin(client: AsyncClient) -> None:
    assert (await client.get(ADMIN_BRANDS)).status_code == 401
