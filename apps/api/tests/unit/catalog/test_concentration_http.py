from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.concentration_status import (
    ArchiveConcentration,
    RestoreConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_concentration import (
    CreateConcentration,
)
from fragancia_api.modules.catalog.application.commands.update_concentration import (
    UpdateConcentration,
)
from fragancia_api.modules.catalog.application.queries.list_concentrations import (
    ListAdminConcentrations,
    ListPublicConcentrations,
)
from fragancia_api.modules.catalog.http.router import routers
from fragancia_api.modules.catalog.infrastructure.in_memory import InMemoryConcentrations
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected

PUBLIC = "/api/v1/concentrations"
ADMIN = "/api/v1/admin/concentrations"

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
def store() -> InMemoryConcentrations:
    return InMemoryConcentrations()


@pytest.fixture
async def client(store: InMemoryConcentrations) -> AsyncIterator[AsyncClient]:
    transactions = InMemoryTransactionRunner()
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        CreateConcentration,
        CreateConcentration(concentrations=store, transactions=transactions, clock=FixedClock()),
    )
    services.add(
        UpdateConcentration, UpdateConcentration(concentrations=store, transactions=transactions)
    )
    services.add(
        ArchiveConcentration, ArchiveConcentration(concentrations=store, transactions=transactions)
    )
    services.add(
        RestoreConcentration, RestoreConcentration(concentrations=store, transactions=transactions)
    )
    services.add(ListPublicConcentrations, ListPublicConcentrations(store))
    services.add(ListAdminConcentrations, ListAdminConcentrations(store))
    app = build_app(services, routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _create(client: AsyncClient, name: str, abbreviation: str) -> str:
    response = await client.post(
        ADMIN, json={"name": name, "abbreviation": abbreviation}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _code(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


# --- access -----------------------------------------------------------------------------------

_SOME_ID = str(UNKNOWN_ID)
_BODY = {"name": "Body Mist", "abbreviation": "Mist"}
_ROUTES = [
    ("GET", ADMIN, None),
    ("POST", ADMIN, _BODY),
    ("PATCH", f"{ADMIN}/{_SOME_ID}", _BODY),
    ("POST", f"{ADMIN}/{_SOME_ID}/archive", None),
    ("POST", f"{ADMIN}/{_SOME_ID}/restore", None),
]


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
async def test_admin_routes_return_401_without_a_session(
    client: AsyncClient, method: str, url: str, body: dict[str, str] | None
) -> None:
    response = await client.request(method, url, json=body)
    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
async def test_admin_routes_return_403_for_an_admin_without_catalog_manage(
    client: AsyncClient, method: str, url: str, body: dict[str, str] | None
) -> None:
    response = await client.request(method, url, json=body, headers=NO_PERMISSION_HEADERS)
    assert _code(response) == (403, "FORBIDDEN")


# --- create -----------------------------------------------------------------------------------


async def test_create_returns_201_and_it_appears_in_the_public_list(client: AsyncClient) -> None:
    response = await client.post(
        ADMIN, json={"name": " Body  Mist ", "abbreviation": "Mist"}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 201
    assert (await client.get(PUBLIC)).json() == [
        {
            "id": response.json()["id"],
            "name": "Body Mist",
            "abbreviation": "Mist",
            "slug": "body-mist",
        }
    ]


async def test_duplicate_name_is_409(client: AsyncClient) -> None:
    await _create(client, "Eau de Toilette", "EDT")

    response = await client.post(
        ADMIN, json={"name": "eau de toilette", "abbreviation": "X1"}, headers=ADMIN_HEADERS
    )

    assert _code(response) == (409, "CATALOG_CONCENTRATION_ALREADY_EXISTS")


async def test_duplicate_abbreviation_is_409(client: AsyncClient) -> None:
    await _create(client, "Eau de Toilette", "EDT")

    response = await client.post(
        ADMIN, json={"name": "Toilette Fraîche", "abbreviation": "edt"}, headers=ADMIN_HEADERS
    )

    assert _code(response) == (409, "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN")


@pytest.mark.parametrize("abbreviation", ["X", "a" * 13])
async def test_invalid_abbreviation_is_422_with_the_domain_code(
    client: AsyncClient, abbreviation: str
) -> None:
    response = await client.post(
        ADMIN, json={"name": "Body Mist", "abbreviation": abbreviation}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "CATALOG_CONCENTRATION_ABBREVIATION_INVALID",
        "message": (
            "A concentration abbreviation needs 2 to 12 characters, including letters or digits"
        ),
        "details": {"min": 2, "max": 12},
    }


async def test_invalid_name_is_422_with_the_domain_code(client: AsyncClient) -> None:
    response = await client.post(
        ADMIN, json={"name": "x", "abbreviation": "Mist"}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "CATALOG_CONCENTRATION_NAME_INVALID",
        "message": "A concentration name needs 2 to 80 characters, including letters or digits",
        "details": {"min": 2, "max": 80},
    }


@pytest.mark.parametrize(
    "body",
    [
        {"name": "a" * 201, "abbreviation": "Mist"},
        {"name": "Body Mist", "abbreviation": "a" * 51},
        {"name": "Body Mist"},
        {"abbreviation": "Mist"},
        {},
    ],
)
async def test_create_payload_is_bounded_and_both_fields_are_required(
    client: AsyncClient, body: dict[str, str]
) -> None:
    response = await client.post(ADMIN, json=body, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


# --- update -----------------------------------------------------------------------------------


async def test_update_returns_204_and_both_lists_show_the_new_texts(
    client: AsyncClient,
) -> None:
    concentration_id = await _create(client, "Body Mist", "Mist")

    response = await client.patch(
        f"{ADMIN}/{concentration_id}",
        json={"name": "Body  Splash", "abbreviation": "Splash"},
        headers=ADMIN_HEADERS,
    )

    assert (response.status_code, response.content) == (204, b"")
    public = (await client.get(PUBLIC)).json()
    admin = (await client.get(ADMIN, headers=ADMIN_HEADERS)).json()
    expected = ("Body Splash", "Splash", "body-splash")
    assert [(c["name"], c["abbreviation"], c["slug"]) for c in public] == [expected]
    assert [(c["name"], c["abbreviation"], c["slug"]) for c in admin["items"]] == [expected]


async def test_update_keeping_the_name_slug_is_204(client: AsyncClient) -> None:
    concentration_id = await _create(client, "Body Mist", "Mist")

    response = await client.patch(
        f"{ADMIN}/{concentration_id}",
        json={"name": "Body Mist", "abbreviation": "BM"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 204


async def test_update_error_cases(client: AsyncClient) -> None:
    mist = await _create(client, "Body Mist", "Mist")
    await _create(client, "Eau de Parfum", "EDP")

    name_taken = await client.patch(
        f"{ADMIN}/{mist}",
        json={"name": "eau de parfum", "abbreviation": "Mist"},
        headers=ADMIN_HEADERS,
    )
    abbreviation_taken = await client.patch(
        f"{ADMIN}/{mist}", json={"name": "Body Mist", "abbreviation": "EDP"}, headers=ADMIN_HEADERS
    )
    unknown = await client.patch(f"{ADMIN}/{UNKNOWN_ID}", json=_BODY, headers=ADMIN_HEADERS)
    invalid_name = await client.patch(
        f"{ADMIN}/{mist}", json={"name": "x", "abbreviation": "Mist"}, headers=ADMIN_HEADERS
    )
    invalid_abbreviation = await client.patch(
        f"{ADMIN}/{mist}", json={"name": "Body Mist", "abbreviation": "X"}, headers=ADMIN_HEADERS
    )

    assert _code(name_taken) == (409, "CATALOG_CONCENTRATION_ALREADY_EXISTS")
    assert _code(abbreviation_taken) == (409, "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN")
    assert _code(unknown) == (404, "CATALOG_CONCENTRATION_NOT_FOUND")
    assert _code(invalid_name) == (422, "CATALOG_CONCENTRATION_NAME_INVALID")
    assert _code(invalid_abbreviation) == (422, "CATALOG_CONCENTRATION_ABBREVIATION_INVALID")


@pytest.mark.parametrize(
    "body",
    [
        {"name": "a" * 201, "abbreviation": "Mist"},
        {"name": "Body Mist", "abbreviation": "a" * 51},
        {"name": "Body Mist"},
        {"abbreviation": "Mist"},
        {},
    ],
)
async def test_update_payload_is_bounded_and_both_fields_are_required(
    client: AsyncClient, body: dict[str, str]
) -> None:
    concentration_id = await _create(client, "Body Mist", "Mist")

    response = await client.patch(f"{ADMIN}/{concentration_id}", json=body, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


async def test_update_rejects_a_malformed_id(client: AsyncClient) -> None:
    response = await client.patch(f"{ADMIN}/not-a-uuid", json=_BODY, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


# --- archive / restore ------------------------------------------------------------------------


async def test_archive_hides_it_publicly_and_restore_shows_it_again(client: AsyncClient) -> None:
    concentration_id = await _create(client, "Body Mist", "Mist")

    archived = await client.post(f"{ADMIN}/{concentration_id}/archive", headers=ADMIN_HEADERS)
    again = await client.post(f"{ADMIN}/{concentration_id}/archive", headers=ADMIN_HEADERS)
    public_after_archive = (await client.get(PUBLIC)).json()
    admin = (await client.get(ADMIN, headers=ADMIN_HEADERS)).json()
    restored = await client.post(f"{ADMIN}/{concentration_id}/restore", headers=ADMIN_HEADERS)
    public_after_restore = (await client.get(PUBLIC)).json()

    assert (archived.status_code, again.status_code, restored.status_code) == (204, 204, 204)
    assert public_after_archive == []
    assert [(c["id"], c["is_active"]) for c in admin["items"]] == [(concentration_id, False)]
    assert [c["id"] for c in public_after_restore] == [concentration_id]


@pytest.mark.parametrize("action", ["archive", "restore"])
async def test_archiving_or_restoring_an_unknown_concentration_is_404(
    client: AsyncClient, action: str
) -> None:
    response = await client.post(f"{ADMIN}/{UNKNOWN_ID}/{action}", headers=ADMIN_HEADERS)

    assert _code(response) == (404, "CATALOG_CONCENTRATION_NOT_FOUND")


# --- lists ------------------------------------------------------------------------------------


async def test_public_list_is_ordered_active_only_and_needs_no_session(
    client: AsyncClient,
) -> None:
    for name, abbreviation in (("Zeta", "ZZ"), ("armada", "AR")):
        await _create(client, name, abbreviation)
    archived = await _create(client, "Old", "OL")
    await client.post(f"{ADMIN}/{archived}/archive", headers=ADMIN_HEADERS)

    response = await client.get(PUBLIC)

    assert response.status_code == 200
    assert [c["name"] for c in response.json()] == ["armada", "Zeta"]
    assert all(set(c) == {"id", "name", "abbreviation", "slug"} for c in response.json())


async def test_admin_list_is_paginated(client: AsyncClient) -> None:
    for name, abbreviation in (("Chipre", "CH"), ("Aromática", "AR"), ("Floral", "FL")):
        await _create(client, name, abbreviation)

    response = await client.get(ADMIN, params={"page": 1, "size": 2}, headers=ADMIN_HEADERS)

    body = response.json()
    assert response.status_code == 200
    assert (body["total"], body["page"], body["size"]) == (3, 1, 2)
    assert [c["name"] for c in body["items"]] == ["Aromática", "Chipre"]
    assert set(body["items"][0]) == {
        "id",
        "name",
        "abbreviation",
        "slug",
        "is_active",
        "created_at",
    }


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}])
async def test_admin_list_rejects_bad_pagination(
    client: AsyncClient, params: dict[str, int]
) -> None:
    response = await client.get(ADMIN, params=params, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


# --- Plan 005: the page number is bounded (MAX_PAGE = 10_000) ------------------------------


async def test_the_last_allowed_page_is_empty_not_an_error(client: AsyncClient) -> None:
    response = await client.get(ADMIN, params={"page": 10_000}, headers=ADMIN_HEADERS)

    body = response.json()
    assert response.status_code == 200
    assert (body["items"], body["page"]) == ([], 10_000)


@pytest.mark.parametrize("page", [10_001, 10**18])
async def test_a_page_past_the_bound_is_a_422_before_any_query(
    client: AsyncClient, page: int
) -> None:
    response = await client.get(ADMIN, params={"page": page}, headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")
