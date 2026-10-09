from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.perfumes import (
    ArchivePerfume,
    CreatePerfume,
    HidePerfume,
    PublishPerfume,
    RestorePerfume,
    UpdatePerfume,
)
from fragancia_api.modules.catalog.application.commands.presentations import (
    AddPresentation,
    ArchivePresentation,
    RestorePresentation,
    UpdatePresentation,
)
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetAdminPerfume,
    ListAdminPerfumes,
)
from fragancia_api.modules.catalog.http.perfume_router import perfume_routers
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected
from tests.unit.catalog.perfume_support import World, make_brand, make_family

ADMIN = "/api/v1/admin/perfumes"
NO_PERMISSION_SESSION = "admin-without-catalog-manage"
NO_PERMISSION_HEADERS = {"Cookie": f"{SESSION_COOKIE}={NO_PERMISSION_SESSION}"}
UNKNOWN_ID = UUID(int=404)
FUTURE = (datetime(2030, 1, 1, tzinfo=UTC)).isoformat()


class _Resolver:
    """The test admin (with `catalog:manage`) plus an admin without any permission."""

    async def resolve(self, token: str) -> Actor | None:
        if token == NO_PERMISSION_SESSION:
            return Actor(id="no-perm", is_admin=True, session_id=UUID(int=2))
        return await TestActorResolver().resolve(token)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
async def client(world: World) -> AsyncIterator[AsyncClient]:
    transactions = InMemoryTransactionRunner()
    clock = FixedClock()
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    for command in (CreatePerfume, UpdatePerfume):
        services.add(
            command,
            command(
                perfumes=world.perfumes,
                brands=world.brands,
                families=world.families,
                concentrations=world.concentrations,
                transactions=transactions,
                clock=clock,
            ),
        )
    for simple in (
        PublishPerfume,
        HidePerfume,
        ArchivePerfume,
        RestorePerfume,
        AddPresentation,
        UpdatePresentation,
        ArchivePresentation,
        RestorePresentation,
    ):
        services.add(
            simple, simple(perfumes=world.perfumes, transactions=transactions, clock=clock)
        )
    services.add(ListAdminPerfumes, ListAdminPerfumes(world.perfumes))
    services.add(GetAdminPerfume, GetAdminPerfume(world.perfumes))
    app = build_app(services, perfume_routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _body(world: World, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "brand_id": str(world.brand.id),
        "concentration_id": str(world.concentration.id),
        "family_id": str(world.family.id),
        "name": "Eros",
        "gender": "men",
        "description": "Fresh and sweet",
        "top_notes": ["Mint", "Green apple"],
        "heart_notes": ["Geranium"],
        "base_notes": ["Vanilla", "Tonka bean"],
    }
    body.update(overrides)
    return body


def _presentation(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ml": 100, "price_cents": 250_000, "availability": "in_stock"}
    body.update(overrides)
    return body


def _code(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _create(client: AsyncClient, world: World, **overrides: Any) -> str:
    response = await client.post(ADMIN, json=_body(world, **overrides), headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _add(client: AsyncClient, perfume_id: str, **overrides: Any) -> str:
    response = await client.post(
        f"{ADMIN}/{perfume_id}/presentations",
        json=_presentation(**overrides),
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _detail(client: AsyncClient, perfume_id: str) -> dict[str, Any]:
    response = await client.get(f"{ADMIN}/{perfume_id}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- access: every one of the 12 routes -------------------------------------------------------

_SOME = str(UNKNOWN_ID)
_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", ADMIN, None),
    ("POST", ADMIN, {}),
    ("GET", f"{ADMIN}/{_SOME}", None),
    ("PUT", f"{ADMIN}/{_SOME}", {}),
    ("POST", f"{ADMIN}/{_SOME}/publish", None),
    ("POST", f"{ADMIN}/{_SOME}/hide", None),
    ("POST", f"{ADMIN}/{_SOME}/archive", None),
    ("POST", f"{ADMIN}/{_SOME}/restore", None),
    ("POST", f"{ADMIN}/{_SOME}/presentations", {}),
    ("PUT", f"{ADMIN}/{_SOME}/presentations/{_SOME}", {}),
    ("POST", f"{ADMIN}/{_SOME}/presentations/{_SOME}/archive", None),
    ("POST", f"{ADMIN}/{_SOME}/presentations/{_SOME}/restore", None),
]


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
async def test_routes_return_401_without_a_session(
    client: AsyncClient, method: str, url: str, body: dict[str, Any] | None
) -> None:
    response = await client.request(method, url, json=body)
    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
async def test_routes_return_403_for_an_admin_without_catalog_manage(
    client: AsyncClient, method: str, url: str, body: dict[str, Any] | None
) -> None:
    response = await client.request(method, url, json=body, headers=NO_PERMISSION_HEADERS)
    assert _code(response) == (403, "FORBIDDEN")


# --- create and detail ------------------------------------------------------------------------


async def test_create_returns_201_and_the_detail_shows_the_slug_and_hidden_state(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)

    detail = await _detail(client, perfume_id)

    assert detail["id"] == perfume_id
    assert detail["slug"] == "versace-eros-edt"
    assert detail["name"] == "Eros"
    assert detail["gender"] == "men"
    assert detail["description"] == "Fresh and sweet"
    assert detail["top_notes"] == ["Mint", "Green apple"]
    assert detail["heart_notes"] == ["Geranium"]
    assert detail["base_notes"] == ["Vanilla", "Tonka bean"]
    assert detail["is_published"] is False
    assert detail["first_published_at"] is None
    assert detail["is_archived"] is False
    assert detail["presentations"] == []
    assert detail["brand"] == {"id": str(world.brand.id), "name": "Versace", "is_active": True}
    assert detail["concentration"] == {
        "id": str(world.concentration.id),
        "name": "Eau de Toilette",
        "abbreviation": "EDT",
        "is_active": True,
    }
    assert detail["family"] == {
        "id": str(world.family.id),
        "name": "Aromática",
        "is_active": True,
    }
    assert set(detail) == {
        "id",
        "slug",
        "name",
        "gender",
        "description",
        "top_notes",
        "heart_notes",
        "base_notes",
        "brand",
        "concentration",
        "family",
        "is_published",
        "first_published_at",
        "is_archived",
        "created_at",
        "updated_at",
        "presentations",
    }


async def test_create_needs_only_the_required_fields(client: AsyncClient, world: World) -> None:
    body = {
        key: value
        for key, value in _body(world).items()
        if key in {"brand_id", "concentration_id", "family_id", "name", "gender"}
    }

    response = await client.post(ADMIN, json=body, headers=ADMIN_HEADERS)

    assert response.status_code == 201
    detail = await _detail(client, response.json()["id"])
    assert (detail["description"], detail["top_notes"]) == ("", [])


async def test_a_duplicate_is_409_and_another_concentration_is_201(
    client: AsyncClient, world: World
) -> None:
    from tests.unit.catalog.perfume_support import make_concentration

    await _create(client, world)
    edp = make_concentration("Eau de Parfum", "EDP")
    world.concentrations.by_id[edp.id] = edp

    duplicate = await client.post(ADMIN, json=_body(world, name="EROS"), headers=ADMIN_HEADERS)
    other = await client.post(
        ADMIN, json=_body(world, concentration_id=str(edp.id)), headers=ADMIN_HEADERS
    )

    assert _code(duplicate) == (409, "CATALOG_PERFUME_ALREADY_EXISTS")
    assert other.status_code == 201
    assert (await _detail(client, other.json()["id"]))["slug"] == "versace-eros-edp"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"name": "x"}, "CATALOG_PERFUME_NAME_INVALID"),
        ({"description": "a" * 2001}, "CATALOG_PERFUME_DESCRIPTION_TOO_LONG"),
        ({"top_notes": ["a"] * 11}, "CATALOG_PERFUME_NOTES_INVALID"),
        ({"heart_notes": ["a" * 41]}, "CATALOG_PERFUME_NOTES_INVALID"),
        ({"base_notes": [" "]}, "CATALOG_PERFUME_NOTES_INVALID"),
        ({"brand_id": str(UNKNOWN_ID)}, "CATALOG_PERFUME_BRAND_UNAVAILABLE"),
        ({"family_id": str(UNKNOWN_ID)}, "CATALOG_PERFUME_FAMILY_UNAVAILABLE"),
        ({"concentration_id": str(UNKNOWN_ID)}, "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"),
    ],
)
async def test_create_domain_errors_are_422_with_their_code(
    client: AsyncClient, world: World, overrides: dict[str, Any], code: str
) -> None:
    response = await client.post(ADMIN, json=_body(world, **overrides), headers=ADMIN_HEADERS)

    assert _code(response) == (422, code)


async def test_create_with_an_archived_reference_is_422(client: AsyncClient, world: World) -> None:
    world.brand.is_active = False

    response = await client.post(ADMIN, json=_body(world), headers=ADMIN_HEADERS)

    assert _code(response) == (422, "CATALOG_PERFUME_BRAND_UNAVAILABLE")


async def test_error_bodies_carry_message_and_details(client: AsyncClient, world: World) -> None:
    response = await client.post(
        ADMIN, json=_body(world, top_notes=["a"] * 11), headers=ADMIN_HEADERS
    )

    assert response.json()["details"] == {"max_per_level": 10, "max_length": 40}
    assert response.json()["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"gender": "other"},
        {"gender": "MEN"},
        {"name": "a" * 201},
        {"description": "a" * 4001},
        {"top_notes": ["a"] * 51},
        {"top_notes": ["a" * 101]},
        {"brand_id": "not-a-uuid"},
        {"top_notes": "Mint"},
    ],
)
async def test_create_payload_is_bounded(
    client: AsyncClient, world: World, overrides: dict[str, Any]
) -> None:
    response = await client.post(ADMIN, json=_body(world, **overrides), headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("missing", ["brand_id", "concentration_id", "family_id", "name", "gender"])
async def test_create_requires_its_main_fields(
    client: AsyncClient, world: World, missing: str
) -> None:
    body = _body(world)
    del body[missing]

    response = await client.post(ADMIN, json=body, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


async def test_the_detail_of_an_unknown_perfume_is_404_and_a_bad_id_is_422(
    client: AsyncClient,
) -> None:
    unknown = await client.get(f"{ADMIN}/{UNKNOWN_ID}", headers=ADMIN_HEADERS)
    malformed = await client.get(f"{ADMIN}/not-a-uuid", headers=ADMIN_HEADERS)

    assert _code(unknown) == (404, "CATALOG_PERFUME_NOT_FOUND")
    assert _code(malformed) == (422, "VALIDATION_ERROR")


# --- update -----------------------------------------------------------------------------------


async def test_update_returns_204_and_recomputes_the_slug(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)

    response = await client.put(
        f"{ADMIN}/{perfume_id}",
        json=_body(world, name="Eros Flame", gender="unisex", base_notes=[]),
        headers=ADMIN_HEADERS,
    )

    assert (response.status_code, response.content) == (204, b"")
    detail = await _detail(client, perfume_id)
    assert (detail["name"], detail["slug"], detail["gender"]) == (
        "Eros Flame",
        "versace-eros-flame-edt",
        "unisex",
    )
    assert detail["base_notes"] == []


async def test_renaming_the_brand_afterwards_does_not_change_the_slug(
    client: AsyncClient, world: World
) -> None:
    from fragancia_api.modules.catalog.domain.brand import BrandName
    from tests.unit.catalog.perfume_support import unwrap_ok

    perfume_id = await _create(client, world)

    world.brand.rename(unwrap_ok(BrandName.create("Versace Home")))

    assert (await _detail(client, perfume_id))["slug"] == "versace-eros-edt"
    assert (await _detail(client, perfume_id))["brand"]["name"] == "Versace Home"


async def test_update_error_cases(client: AsyncClient, world: World) -> None:
    perfume_id = await _create(client, world)
    other = await _create(client, world, name="Eros Flame")
    fresh = make_family("Floral", active=False)
    world.families.by_id[fresh.id] = fresh

    unknown = await client.put(f"{ADMIN}/{UNKNOWN_ID}", json=_body(world), headers=ADMIN_HEADERS)
    taken = await client.put(
        f"{ADMIN}/{other}", json=_body(world, name="Eros"), headers=ADMIN_HEADERS
    )
    invalid = await client.put(
        f"{ADMIN}/{perfume_id}", json=_body(world, name="x"), headers=ADMIN_HEADERS
    )
    archived_family = await client.put(
        f"{ADMIN}/{perfume_id}", json=_body(world, family_id=str(fresh.id)), headers=ADMIN_HEADERS
    )
    bad_payload = await client.put(
        f"{ADMIN}/{perfume_id}", json=_body(world, gender="x"), headers=ADMIN_HEADERS
    )

    assert _code(unknown) == (404, "CATALOG_PERFUME_NOT_FOUND")
    assert _code(taken) == (409, "CATALOG_PERFUME_ALREADY_EXISTS")
    assert _code(invalid) == (422, "CATALOG_PERFUME_NAME_INVALID")
    assert _code(archived_family) == (422, "CATALOG_PERFUME_FAMILY_UNAVAILABLE")
    assert _code(bad_payload) == (422, "VALIDATION_ERROR")


async def test_update_keeps_an_unchanged_archived_family_and_can_change_it_to_an_active_one(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)
    world.family.is_active = False
    active = make_family("Floral")
    world.families.by_id[active.id] = active

    kept = await client.put(
        f"{ADMIN}/{perfume_id}", json=_body(world, description="Kept"), headers=ADMIN_HEADERS
    )
    changed = await client.put(
        f"{ADMIN}/{perfume_id}", json=_body(world, family_id=str(active.id)), headers=ADMIN_HEADERS
    )

    assert (kept.status_code, changed.status_code) == (204, 204)
    assert (await _detail(client, perfume_id))["family"]["id"] == str(active.id)


# --- presentations ----------------------------------------------------------------------------


async def test_add_presentation_returns_201_and_the_detail_orders_them_by_ml(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)

    big = await _add(
        client,
        perfume_id,
        ml=200,
        price_cents=390_000,
        sale_price_cents=350_000,
        sale_ends_at=FUTURE,
        availability="made_to_order",
        lead_time_min_days=7,
        lead_time_max_days=10,
    )
    small = await _add(client, perfume_id)

    detail = await _detail(client, perfume_id)
    assert [p["id"] for p in detail["presentations"]] == [small, big]
    assert detail["presentations"][0] == {
        "id": small,
        "ml": 100,
        "price_cents": 250_000,
        "sale_price_cents": None,
        "sale_starts_at": None,
        "sale_ends_at": None,
        "availability": "in_stock",
        "lead_time_min_days": None,
        "lead_time_max_days": None,
        "is_active": True,
        "created_at": detail["presentations"][0]["created_at"],
    }
    sale = detail["presentations"][1]
    assert (sale["price_cents"], sale["sale_price_cents"]) == (390_000, 350_000)
    assert datetime.fromisoformat(sale["sale_ends_at"]) == datetime.fromisoformat(FUTURE)
    assert (sale["availability"], sale["lead_time_min_days"], sale["lead_time_max_days"]) == (
        "made_to_order",
        7,
        10,
    )


async def test_add_presentation_conflicts_and_missing_perfume(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)
    await _add(client, perfume_id)

    duplicate = await client.post(
        f"{ADMIN}/{perfume_id}/presentations", json=_presentation(), headers=ADMIN_HEADERS
    )
    unknown = await client.post(
        f"{ADMIN}/{UNKNOWN_ID}/presentations", json=_presentation(), headers=ADMIN_HEADERS
    )

    assert _code(duplicate) == (409, "CATALOG_PRESENTATION_ALREADY_EXISTS")
    assert _code(unknown) == (404, "CATALOG_PERFUME_NOT_FOUND")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"ml": 0}, "CATALOG_PRESENTATION_ML_INVALID"),
        ({"ml": 1001}, "CATALOG_PRESENTATION_ML_INVALID"),
        ({"price_cents": 0}, "CATALOG_PRESENTATION_PRICE_INVALID"),
        ({"price_cents": 10_000_001}, "CATALOG_PRESENTATION_PRICE_INVALID"),
        ({"sale_price_cents": 250_000}, "CATALOG_PRESENTATION_SALE_INVALID"),
        ({"sale_price_cents": 300_000}, "CATALOG_PRESENTATION_SALE_INVALID"),
        (
            {
                "sale_price_cents": 100,
                "sale_starts_at": "2030-01-02T00:00:00Z",
                "sale_ends_at": "2030-01-02T00:00:00Z",
            },
            "CATALOG_PRESENTATION_SALE_INVALID",
        ),
        ({"availability": "made_to_order"}, "CATALOG_PRESENTATION_AVAILABILITY_INVALID"),
        (
            {
                "availability": "made_to_order",
                "lead_time_min_days": 10,
                "lead_time_max_days": 7,
            },
            "CATALOG_PRESENTATION_AVAILABILITY_INVALID",
        ),
        (
            {"availability": "in_stock", "lead_time_min_days": 1, "lead_time_max_days": 2},
            "CATALOG_PRESENTATION_AVAILABILITY_INVALID",
        ),
    ],
)
async def test_presentation_domain_errors_are_422_with_their_code(
    client: AsyncClient, world: World, overrides: dict[str, Any], code: str
) -> None:
    perfume_id = await _create(client, world)

    response = await client.post(
        f"{ADMIN}/{perfume_id}/presentations",
        json=_presentation(**overrides),
        headers=ADMIN_HEADERS,
    )

    assert _code(response) == (422, code)


@pytest.mark.parametrize(
    "overrides",
    [
        {"availability": "on_demand"},
        {"sale_price_cents": 100, "sale_ends_at": "2030-01-01T00:00:00"},  # naive datetime
        {"sale_price_cents": 100, "sale_starts_at": "yesterday"},
        {"ml": "big"},
        {"price_cents": 12.5},
    ],
)
async def test_presentation_payload_is_bounded(
    client: AsyncClient, world: World, overrides: dict[str, Any]
) -> None:
    perfume_id = await _create(client, world)

    response = await client.post(
        f"{ADMIN}/{perfume_id}/presentations",
        json=_presentation(**overrides),
        headers=ADMIN_HEADERS,
    )

    assert _code(response) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("missing", ["ml", "price_cents", "availability"])
async def test_presentation_requires_its_main_fields(
    client: AsyncClient, world: World, missing: str
) -> None:
    perfume_id = await _create(client, world)
    body = _presentation()
    del body[missing]

    response = await client.post(
        f"{ADMIN}/{perfume_id}/presentations", json=body, headers=ADMIN_HEADERS
    )

    assert _code(response) == (422, "VALIDATION_ERROR")


async def test_update_presentation_returns_204_and_replaces_its_fields(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)
    presentation_id = await _add(client, perfume_id)

    response = await client.put(
        f"{ADMIN}/{perfume_id}/presentations/{presentation_id}",
        json=_presentation(ml=150, price_cents=300_000, sale_price_cents=1),
        headers=ADMIN_HEADERS,
    )

    assert (response.status_code, response.content) == (204, b"")
    [presentation] = (await _detail(client, perfume_id))["presentations"]
    assert (presentation["ml"], presentation["price_cents"], presentation["sale_price_cents"]) == (
        150,
        300_000,
        1,
    )


async def test_update_presentation_error_cases(client: AsyncClient, world: World) -> None:
    perfume_id = await _create(client, world)
    await _add(client, perfume_id, ml=100)
    second = await _add(client, perfume_id, ml=200)
    url = f"{ADMIN}/{perfume_id}/presentations"

    taken = await client.put(f"{url}/{second}", json=_presentation(ml=100), headers=ADMIN_HEADERS)
    unknown_presentation = await client.put(
        f"{url}/{UNKNOWN_ID}", json=_presentation(), headers=ADMIN_HEADERS
    )
    unknown_perfume = await client.put(
        f"{ADMIN}/{UNKNOWN_ID}/presentations/{second}", json=_presentation(), headers=ADMIN_HEADERS
    )
    invalid = await client.put(f"{url}/{second}", json=_presentation(ml=0), headers=ADMIN_HEADERS)
    malformed = await client.put(f"{url}/not-a-uuid", json=_presentation(), headers=ADMIN_HEADERS)

    assert _code(taken) == (409, "CATALOG_PRESENTATION_ALREADY_EXISTS")
    assert _code(unknown_presentation) == (404, "CATALOG_PRESENTATION_NOT_FOUND")
    assert _code(unknown_perfume) == (404, "CATALOG_PERFUME_NOT_FOUND")
    assert _code(invalid) == (422, "CATALOG_PRESENTATION_ML_INVALID")
    assert _code(malformed) == (422, "VALIDATION_ERROR")


async def test_archive_and_restore_a_presentation(client: AsyncClient, world: World) -> None:
    perfume_id = await _create(client, world)
    presentation_id = await _add(client, perfume_id)
    url = f"{ADMIN}/{perfume_id}/presentations/{presentation_id}"

    archived = await client.post(f"{url}/archive", headers=ADMIN_HEADERS)
    archived_again = await client.post(f"{url}/archive", headers=ADMIN_HEADERS)
    state_archived = (await _detail(client, perfume_id))["presentations"][0]["is_active"]
    restored = await client.post(f"{url}/restore", headers=ADMIN_HEADERS)
    restored_again = await client.post(f"{url}/restore", headers=ADMIN_HEADERS)

    assert [r.status_code for r in (archived, archived_again, restored, restored_again)] == [
        204
    ] * 4
    assert state_archived is False
    assert (await _detail(client, perfume_id))["presentations"][0]["is_active"] is True


@pytest.mark.parametrize("action", ["archive", "restore"])
async def test_presentation_archive_and_restore_unknown_ids_are_404(
    client: AsyncClient, world: World, action: str
) -> None:
    perfume_id = await _create(client, world)

    unknown_presentation = await client.post(
        f"{ADMIN}/{perfume_id}/presentations/{UNKNOWN_ID}/{action}", headers=ADMIN_HEADERS
    )
    unknown_perfume = await client.post(
        f"{ADMIN}/{UNKNOWN_ID}/presentations/{UNKNOWN_ID}/{action}", headers=ADMIN_HEADERS
    )

    assert _code(unknown_presentation) == (404, "CATALOG_PRESENTATION_NOT_FOUND")
    assert _code(unknown_perfume) == (404, "CATALOG_PERFUME_NOT_FOUND")


# --- publish, hide, archive, restore ----------------------------------------------------------


async def test_publish_without_presentations_is_422(client: AsyncClient, world: World) -> None:
    perfume_id = await _create(client, world)

    response = await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS)

    assert _code(response) == (422, "CATALOG_PERFUME_NOTHING_TO_SELL")


async def test_publish_hide_publish_keeps_the_first_publication_date(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)
    await _add(client, perfume_id)

    published = await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS)
    first = (await _detail(client, perfume_id))["first_published_at"]
    again = await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS)
    hidden = await client.post(f"{ADMIN}/{perfume_id}/hide", headers=ADMIN_HEADERS)
    hidden_again = await client.post(f"{ADMIN}/{perfume_id}/hide", headers=ADMIN_HEADERS)
    state_hidden = await _detail(client, perfume_id)
    republished = await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS)
    final = await _detail(client, perfume_id)

    assert [r.status_code for r in (published, again, hidden, hidden_again, republished)] == [
        204
    ] * 5
    assert first is not None
    assert state_hidden["is_published"] is False
    assert state_hidden["first_published_at"] == first
    assert (final["is_published"], final["first_published_at"]) == (True, first)


async def test_the_last_active_presentation_of_a_published_perfume_is_409(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)
    small = await _add(client, perfume_id, ml=100)
    big = await _add(client, perfume_id, ml=200)
    await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS)
    url = f"{ADMIN}/{perfume_id}/presentations"

    first = await client.post(f"{url}/{big}/archive", headers=ADMIN_HEADERS)
    last = await client.post(f"{url}/{small}/archive", headers=ADMIN_HEADERS)
    await client.post(f"{ADMIN}/{perfume_id}/hide", headers=ADMIN_HEADERS)
    after_hide = await client.post(f"{url}/{small}/archive", headers=ADMIN_HEADERS)

    assert first.status_code == 204
    assert _code(last) == (409, "CATALOG_PERFUME_LAST_PRESENTATION")
    assert after_hide.status_code == 204


async def test_archive_hides_the_perfume_makes_it_read_only_and_restore_keeps_it_hidden(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)
    presentation_id = await _add(client, perfume_id)
    await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS)

    archived = await client.post(f"{ADMIN}/{perfume_id}/archive", headers=ADMIN_HEADERS)
    archived_again = await client.post(f"{ADMIN}/{perfume_id}/archive", headers=ADMIN_HEADERS)
    state = await _detail(client, perfume_id)
    refused = [
        await client.put(f"{ADMIN}/{perfume_id}", json=_body(world), headers=ADMIN_HEADERS),
        await client.post(f"{ADMIN}/{perfume_id}/publish", headers=ADMIN_HEADERS),
        await client.post(
            f"{ADMIN}/{perfume_id}/presentations", json=_presentation(ml=50), headers=ADMIN_HEADERS
        ),
        await client.put(
            f"{ADMIN}/{perfume_id}/presentations/{presentation_id}",
            json=_presentation(),
            headers=ADMIN_HEADERS,
        ),
        await client.post(
            f"{ADMIN}/{perfume_id}/presentations/{presentation_id}/archive", headers=ADMIN_HEADERS
        ),
        await client.post(
            f"{ADMIN}/{perfume_id}/presentations/{presentation_id}/restore", headers=ADMIN_HEADERS
        ),
    ]
    hide_archived = await client.post(f"{ADMIN}/{perfume_id}/hide", headers=ADMIN_HEADERS)
    restored = await client.post(f"{ADMIN}/{perfume_id}/restore", headers=ADMIN_HEADERS)
    after = await _detail(client, perfume_id)

    assert (archived.status_code, archived_again.status_code) == (204, 204)
    assert (state["is_archived"], state["is_published"]) == (True, False)
    assert [_code(r) for r in refused] == [(422, "CATALOG_PERFUME_ARCHIVED")] * 6
    assert hide_archived.status_code == 204
    assert restored.status_code == 204
    assert (after["is_archived"], after["is_published"]) == (False, False)


@pytest.mark.parametrize("action", ["publish", "hide", "archive", "restore"])
async def test_status_actions_on_an_unknown_perfume_are_404(
    client: AsyncClient, action: str
) -> None:
    response = await client.post(f"{ADMIN}/{UNKNOWN_ID}/{action}", headers=ADMIN_HEADERS)

    assert _code(response) == (404, "CATALOG_PERFUME_NOT_FOUND")


# --- list -------------------------------------------------------------------------------------


async def test_the_list_is_ordered_by_brand_then_name_with_the_active_presentation_count(
    client: AsyncClient, world: World
) -> None:
    dior = make_brand("dior")
    world.brands.by_id[dior.id] = dior
    await _create(client, world, name="Sauvage", brand_id=str(dior.id))
    flame = await _create(client, world, name="eros Flame")
    await _create(client, world, name="Dylan Blue")
    one = await _add(client, flame, ml=100)
    await _add(client, flame, ml=200)
    await client.post(f"{ADMIN}/{flame}/presentations/{one}/archive", headers=ADMIN_HEADERS)

    response = await client.get(ADMIN, headers=ADMIN_HEADERS)

    body = response.json()
    assert response.status_code == 200
    assert (body["total"], body["page"]) == (3, 1)
    assert [(p["brand"]["name"], p["name"]) for p in body["items"]] == [
        ("dior", "Sauvage"),
        ("Versace", "Dylan Blue"),
        ("Versace", "eros Flame"),
    ]
    assert [p["active_presentations"] for p in body["items"]] == [0, 0, 1]
    assert set(body["items"][0]) == {
        "id",
        "slug",
        "name",
        "brand",
        "concentration",
        "gender",
        "is_published",
        "is_archived",
        "active_presentations",
        "created_at",
    }


async def test_the_list_hides_archived_perfumes_unless_asked(
    client: AsyncClient, world: World
) -> None:
    keep = await _create(client, world, name="Eros")
    gone = await _create(client, world, name="Eros Flame")
    await client.post(f"{ADMIN}/{gone}/archive", headers=ADMIN_HEADERS)

    default = (await client.get(ADMIN, headers=ADMIN_HEADERS)).json()
    archived = (await client.get(ADMIN, params={"archived": "true"}, headers=ADMIN_HEADERS)).json()

    assert [p["id"] for p in default["items"]] == [keep]
    assert default["total"] == 1
    assert [p["id"] for p in archived["items"]] == [gone]
    assert archived["items"][0]["is_archived"] is True
    assert archived["total"] == 1


async def test_the_list_is_paginated(client: AsyncClient, world: World) -> None:
    for name in ("Alpha", "Bravo", "Charlie"):
        await _create(client, world, name=name)

    response = await client.get(ADMIN, params={"page": 2, "size": 2}, headers=ADMIN_HEADERS)

    body = response.json()
    assert (body["total"], body["page"], body["size"]) == (3, 2, 2)
    assert [p["name"] for p in body["items"]] == ["Charlie"]


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}, {"archived": "maybe"}])
async def test_the_list_rejects_bad_parameters(client: AsyncClient, params: dict[str, Any]) -> None:
    response = await client.get(ADMIN, params=params, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


async def test_the_default_creation_time_is_the_clock_and_timestamps_are_iso(
    client: AsyncClient, world: World
) -> None:
    perfume_id = await _create(client, world)

    detail = await _detail(client, perfume_id)

    assert datetime.fromisoformat(detail["created_at"]) == FixedClock().now()
    assert datetime.fromisoformat(detail["updated_at"]) - datetime.fromisoformat(
        detail["created_at"]
    ) == timedelta(0)
