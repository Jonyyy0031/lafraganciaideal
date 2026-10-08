from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.brand_status import (
    ArchiveBrand,
    RestoreBrand,
)
from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.commands.create_olfactory_family import (
    CreateOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.olfactory_family_status import (
    ArchiveOlfactoryFamily,
    RestoreOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.rename_brand import RenameBrand
from fragancia_api.modules.catalog.application.commands.rename_olfactory_family import (
    RenameOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.application.queries.list_olfactory_families import (
    ListAdminOlfactoryFamilies,
    ListPublicOlfactoryFamilies,
)
from fragancia_api.modules.catalog.http.router import routers
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryBrands,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected

BRANDS = "/api/v1/brands"
ADMIN_BRANDS = "/api/v1/admin/brands"
FAMILIES = "/api/v1/olfactory-families"
ADMIN_FAMILIES = "/api/v1/admin/olfactory-families"

NO_PERMISSION_SESSION = "admin-without-catalog-manage"
NO_PERMISSION_HEADERS = {"Cookie": f"{SESSION_COOKIE}={NO_PERMISSION_SESSION}"}
UNKNOWN_ID = UUID(int=404)


class _Resolver:
    """The test admin (with `catalog:manage`) plus an admin without any permission."""

    async def resolve(self, token: str) -> Actor | None:
        if token == NO_PERMISSION_SESSION:
            return Actor(id="no-perm", is_admin=True, session_id=UUID(int=2))
        return await TestActorResolver().resolve(token)


@pytest.fixture
def brand_store() -> InMemoryBrands:
    return InMemoryBrands()


@pytest.fixture
def family_store() -> InMemoryOlfactoryFamilies:
    return InMemoryOlfactoryFamilies()


@pytest.fixture
async def client(
    brand_store: InMemoryBrands, family_store: InMemoryOlfactoryFamilies
) -> AsyncIterator[AsyncClient]:
    transactions = InMemoryTransactionRunner()
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        CreateBrand,
        CreateBrand(
            brands=brand_store,
            transactions=transactions,
            events=RecordingEventPublisher(),
            clock=FixedClock(),
        ),
    )
    services.add(RenameBrand, RenameBrand(brands=brand_store, transactions=transactions))
    services.add(ArchiveBrand, ArchiveBrand(brands=brand_store, transactions=transactions))
    services.add(RestoreBrand, RestoreBrand(brands=brand_store, transactions=transactions))
    services.add(ListPublicBrands, ListPublicBrands(brand_store))
    services.add(ListAdminBrands, ListAdminBrands(brand_store))
    services.add(
        CreateOlfactoryFamily,
        CreateOlfactoryFamily(families=family_store, transactions=transactions, clock=FixedClock()),
    )
    services.add(
        RenameOlfactoryFamily,
        RenameOlfactoryFamily(families=family_store, transactions=transactions),
    )
    services.add(
        ArchiveOlfactoryFamily,
        ArchiveOlfactoryFamily(families=family_store, transactions=transactions),
    )
    services.add(
        RestoreOlfactoryFamily,
        RestoreOlfactoryFamily(families=family_store, transactions=transactions),
    )
    services.add(ListPublicOlfactoryFamilies, ListPublicOlfactoryFamilies(family_store))
    services.add(ListAdminOlfactoryFamilies, ListAdminOlfactoryFamilies(family_store))
    app = build_app(services, routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _create(client: AsyncClient, url: str, name: str) -> str:
    response = await client.post(url, json={"name": name}, headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _code(response) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    return response.status_code, response.json()["code"]


# --- access: every new admin route -----------------------------------------------------------

_SOME_ID = str(UNKNOWN_ID)
_ROUTES = [
    ("PATCH", f"{ADMIN_BRANDS}/{_SOME_ID}", {"name": "Dior"}),
    ("POST", f"{ADMIN_BRANDS}/{_SOME_ID}/archive", None),
    ("POST", f"{ADMIN_BRANDS}/{_SOME_ID}/restore", None),
    ("GET", ADMIN_FAMILIES, None),
    ("POST", ADMIN_FAMILIES, {"name": "Floral"}),
    ("PATCH", f"{ADMIN_FAMILIES}/{_SOME_ID}", {"name": "Floral"}),
    ("POST", f"{ADMIN_FAMILIES}/{_SOME_ID}/archive", None),
    ("POST", f"{ADMIN_FAMILIES}/{_SOME_ID}/restore", None),
]


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
async def test_new_admin_routes_return_401_without_a_session(
    client: AsyncClient, method: str, url: str, body: dict[str, str] | None
) -> None:
    response = await client.request(method, url, json=body)
    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
async def test_new_admin_routes_return_403_for_an_admin_without_catalog_manage(
    client: AsyncClient, method: str, url: str, body: dict[str, str] | None
) -> None:
    response = await client.request(method, url, json=body, headers=NO_PERMISSION_HEADERS)
    assert _code(response) == (403, "FORBIDDEN")


@pytest.mark.parametrize(
    ("method", "url"),
    [("POST", ADMIN_BRANDS), ("GET", ADMIN_BRANDS)],
)
async def test_existing_brand_admin_routes_also_require_catalog_manage(
    client: AsyncClient, method: str, url: str
) -> None:
    response = await client.request(
        method, url, json={"name": "Dior"}, headers=NO_PERMISSION_HEADERS
    )
    assert _code(response) == (403, "FORBIDDEN")


# --- brands -----------------------------------------------------------------------------------


async def test_rename_brand_returns_204_and_the_slug_follows(
    client: AsyncClient, brand_store: InMemoryBrands
) -> None:
    brand_id = await _create(client, ADMIN_BRANDS, "Dior")

    response = await client.patch(
        f"{ADMIN_BRANDS}/{brand_id}", json={"name": " Christian  Dior"}, headers=ADMIN_HEADERS
    )

    assert (response.status_code, response.content) == (204, b"")
    assert brand_store.by_id[UUID(brand_id)].slug == "christian-dior"


async def test_rename_brand_to_the_same_slug_is_204(client: AsyncClient) -> None:
    brand_id = await _create(client, ADMIN_BRANDS, "Dior")

    response = await client.patch(
        f"{ADMIN_BRANDS}/{brand_id}", json={"name": "DIOR"}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 204


async def test_rename_brand_to_another_brands_name_is_409(client: AsyncClient) -> None:
    brand_id = await _create(client, ADMIN_BRANDS, "Dior")
    await _create(client, ADMIN_BRANDS, "Chanel")

    response = await client.patch(
        f"{ADMIN_BRANDS}/{brand_id}", json={"name": "chanel"}, headers=ADMIN_HEADERS
    )

    assert _code(response) == (409, "CATALOG_BRAND_ALREADY_EXISTS")


async def test_rename_unknown_brand_is_404(client: AsyncClient) -> None:
    response = await client.patch(
        f"{ADMIN_BRANDS}/{UNKNOWN_ID}", json={"name": "Dior"}, headers=ADMIN_HEADERS
    )
    assert _code(response) == (404, "CATALOG_BRAND_NOT_FOUND")


async def test_rename_brand_with_an_invalid_name_is_422_with_the_domain_code(
    client: AsyncClient,
) -> None:
    brand_id = await _create(client, ADMIN_BRANDS, "Dior")

    response = await client.patch(
        f"{ADMIN_BRANDS}/{brand_id}", json={"name": "x"}, headers=ADMIN_HEADERS
    )

    assert _code(response) == (422, "CATALOG_BRAND_NAME_INVALID")


async def test_rename_brand_rejects_an_oversized_payload_and_a_bad_id(client: AsyncClient) -> None:
    brand_id = await _create(client, ADMIN_BRANDS, "Dior")

    too_long = await client.patch(
        f"{ADMIN_BRANDS}/{brand_id}", json={"name": "a" * 201}, headers=ADMIN_HEADERS
    )
    bad_id = await client.patch(
        f"{ADMIN_BRANDS}/not-a-uuid", json={"name": "Dior"}, headers=ADMIN_HEADERS
    )
    missing = await client.patch(f"{ADMIN_BRANDS}/{brand_id}", json={}, headers=ADMIN_HEADERS)

    assert _code(too_long) == (422, "VALIDATION_ERROR")
    assert _code(bad_id) == (422, "VALIDATION_ERROR")
    assert _code(missing) == (422, "VALIDATION_ERROR")


async def test_archive_hides_a_brand_from_the_public_list_and_restore_brings_it_back(
    client: AsyncClient,
) -> None:
    brand_id = await _create(client, ADMIN_BRANDS, "Dior")

    archived = await client.post(f"{ADMIN_BRANDS}/{brand_id}/archive", headers=ADMIN_HEADERS)
    again = await client.post(f"{ADMIN_BRANDS}/{brand_id}/archive", headers=ADMIN_HEADERS)
    public_after_archive = (await client.get(BRANDS)).json()
    admin = (await client.get(ADMIN_BRANDS, headers=ADMIN_HEADERS)).json()
    restored = await client.post(f"{ADMIN_BRANDS}/{brand_id}/restore", headers=ADMIN_HEADERS)
    public_after_restore = (await client.get(BRANDS)).json()

    assert (archived.status_code, again.status_code, restored.status_code) == (204, 204, 204)
    assert public_after_archive == []
    assert [(b["id"], b["is_active"]) for b in admin["items"]] == [(brand_id, False)]
    assert [b["id"] for b in public_after_restore] == [brand_id]


@pytest.mark.parametrize("action", ["archive", "restore"])
async def test_archiving_or_restoring_an_unknown_brand_is_404(
    client: AsyncClient, action: str
) -> None:
    response = await client.post(f"{ADMIN_BRANDS}/{UNKNOWN_ID}/{action}", headers=ADMIN_HEADERS)
    assert _code(response) == (404, "CATALOG_BRAND_NOT_FOUND")


# --- olfactory families -----------------------------------------------------------------------


async def test_create_family_returns_201_and_it_appears_in_the_public_list(
    client: AsyncClient,
) -> None:
    response = await client.post(
        ADMIN_FAMILIES, json={"name": "  Especiada "}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 201
    family_id = response.json()["id"]
    public = (await client.get(FAMILIES)).json()
    assert public == [{"id": family_id, "name": "Especiada", "slug": "especiada"}]


async def test_duplicate_family_is_409(client: AsyncClient) -> None:
    await _create(client, ADMIN_FAMILIES, "Cítrica")

    response = await client.post(ADMIN_FAMILIES, json={"name": "citrica"}, headers=ADMIN_HEADERS)

    assert _code(response) == (409, "CATALOG_FAMILY_ALREADY_EXISTS")


async def test_invalid_family_name_is_422_with_the_domain_code(client: AsyncClient) -> None:
    response = await client.post(ADMIN_FAMILIES, json={"name": "x"}, headers=ADMIN_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "code": "CATALOG_FAMILY_NAME_INVALID",
        "message": "An olfactory family name needs 2 to 80 characters, including letters or digits",
        "details": {"min": 2, "max": 80},
    }


async def test_family_payload_is_bounded_and_required(client: AsyncClient) -> None:
    too_long = await client.post(ADMIN_FAMILIES, json={"name": "a" * 201}, headers=ADMIN_HEADERS)
    missing = await client.post(ADMIN_FAMILIES, json={}, headers=ADMIN_HEADERS)

    assert _code(too_long) == (422, "VALIDATION_ERROR")
    assert _code(missing) == (422, "VALIDATION_ERROR")


async def test_rename_family_returns_204_and_both_lists_show_the_new_slug(
    client: AsyncClient,
) -> None:
    family_id = await _create(client, ADMIN_FAMILIES, "Especiada")

    response = await client.patch(
        f"{ADMIN_FAMILIES}/{family_id}", json={"name": "Especiada Cálida"}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 204
    public = (await client.get(FAMILIES)).json()
    admin = (await client.get(ADMIN_FAMILIES, headers=ADMIN_HEADERS)).json()
    assert [(f["name"], f["slug"]) for f in public] == [("Especiada Cálida", "especiada-calida")]
    assert [(f["name"], f["slug"]) for f in admin["items"]] == [
        ("Especiada Cálida", "especiada-calida")
    ]


async def test_rename_family_error_cases(client: AsyncClient) -> None:
    floral = await _create(client, ADMIN_FAMILIES, "Floral")
    await _create(client, ADMIN_FAMILIES, "Chipre")

    same_slug = await client.patch(
        f"{ADMIN_FAMILIES}/{floral}", json={"name": "FLORAL"}, headers=ADMIN_HEADERS
    )
    taken = await client.patch(
        f"{ADMIN_FAMILIES}/{floral}", json={"name": "chipre"}, headers=ADMIN_HEADERS
    )
    unknown = await client.patch(
        f"{ADMIN_FAMILIES}/{UNKNOWN_ID}", json={"name": "Floral"}, headers=ADMIN_HEADERS
    )
    invalid = await client.patch(
        f"{ADMIN_FAMILIES}/{floral}", json={"name": "x"}, headers=ADMIN_HEADERS
    )

    assert same_slug.status_code == 204
    assert _code(taken) == (409, "CATALOG_FAMILY_ALREADY_EXISTS")
    assert _code(unknown) == (404, "CATALOG_FAMILY_NOT_FOUND")
    assert _code(invalid) == (422, "CATALOG_FAMILY_NAME_INVALID")


async def test_family_archive_hides_it_publicly_and_restore_shows_it_again(
    client: AsyncClient,
) -> None:
    family_id = await _create(client, ADMIN_FAMILIES, "Floral")

    archived = await client.post(f"{ADMIN_FAMILIES}/{family_id}/archive", headers=ADMIN_HEADERS)
    again = await client.post(f"{ADMIN_FAMILIES}/{family_id}/archive", headers=ADMIN_HEADERS)
    public_after_archive = (await client.get(FAMILIES)).json()
    admin = (await client.get(ADMIN_FAMILIES, headers=ADMIN_HEADERS)).json()
    restored = await client.post(f"{ADMIN_FAMILIES}/{family_id}/restore", headers=ADMIN_HEADERS)
    public_after_restore = (await client.get(FAMILIES)).json()

    assert (archived.status_code, again.status_code, restored.status_code) == (204, 204, 204)
    assert public_after_archive == []
    assert [(f["id"], f["is_active"]) for f in admin["items"]] == [(family_id, False)]
    assert [f["id"] for f in public_after_restore] == [family_id]


@pytest.mark.parametrize("action", ["archive", "restore"])
async def test_archiving_or_restoring_an_unknown_family_is_404(
    client: AsyncClient, action: str
) -> None:
    response = await client.post(f"{ADMIN_FAMILIES}/{UNKNOWN_ID}/{action}", headers=ADMIN_HEADERS)
    assert _code(response) == (404, "CATALOG_FAMILY_NOT_FOUND")


async def test_public_family_list_is_ordered_and_needs_no_session(client: AsyncClient) -> None:
    for name in ("Zeta", "armada"):
        await _create(client, ADMIN_FAMILIES, name)

    response = await client.get(FAMILIES)

    assert response.status_code == 200
    assert [f["name"] for f in response.json()] == ["armada", "Zeta"]
    assert all(set(f) == {"id", "name", "slug"} for f in response.json())


async def test_admin_family_list_is_paginated(client: AsyncClient) -> None:
    for name in ("Chipre", "Aromática", "Floral"):
        await _create(client, ADMIN_FAMILIES, name)

    response = await client.get(
        ADMIN_FAMILIES, params={"page": 1, "size": 2}, headers=ADMIN_HEADERS
    )

    body = response.json()
    assert response.status_code == 200
    assert (body["total"], body["page"], body["size"]) == (3, 1, 2)
    assert [f["name"] for f in body["items"]] == ["Aromática", "Chipre"]
    assert set(body["items"][0]) == {"id", "name", "slug", "is_active", "created_at"}


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}])
async def test_admin_family_list_rejects_bad_pagination(
    client: AsyncClient, params: dict[str, int]
) -> None:
    response = await client.get(ADMIN_FAMILIES, params=params, headers=ADMIN_HEADERS)
    assert _code(response) == (422, "VALIDATION_ERROR")
