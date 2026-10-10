"""Plan 004: the public perfume routes (no session needed)."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetPublicPerfume,
    ListPublicPerfumes,
)
from fragancia_api.modules.catalog.contracts import PublicPerfume, PublicPerfumePage
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Ml,
    Price,
    Sale,
)
from fragancia_api.modules.catalog.http.perfume_router import perfume_routers
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Money
from tests.support import TestActorResolver, assert_admin_routes_are_protected
from tests.unit.catalog.perfume_support import NOW, World, add_presentation, make_perfume, unwrap_ok

PUBLIC = "/api/v1/perfumes"


class StubQueries:
    """Records the filters and answers an empty page; `get_public` finds nothing."""

    def __init__(self) -> None:
        self.filters: list[PublicPerfumeFilters] = []

    async def list_public(
        self, filters: PublicPerfumeFilters, *, now: datetime
    ) -> PublicPerfumePage:
        self.filters.append(filters)
        return PublicPerfumePage(items=[], total=0, page=filters.page, size=filters.size)

    async def get_public(self, slug: str, *, now: datetime) -> PublicPerfume | None:
        return None


@pytest.fixture
def stub() -> StubQueries:
    return StubQueries()


def _client(queries: Any) -> AsyncClient:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    clock = FixedClock()
    services.add(ListPublicPerfumes, ListPublicPerfumes(queries, clock))
    services.add(GetPublicPerfume, GetPublicPerfume(queries, clock))
    app = build_app(services, perfume_routers)
    assert_admin_routes_are_protected(app)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
async def client(stub: StubQueries) -> AsyncIterator[AsyncClient]:
    async with _client(stub) as c:
        yield c


@pytest.fixture
async def world_client() -> AsyncIterator[tuple[AsyncClient, World]]:
    world = World()
    async with _client(world.perfumes) as c:
        yield c, world


# --- GET /perfumes: query parsing -------------------------------------------------------------


async def test_the_list_needs_no_session_and_defaults_to_name_page_1_size_24(
    client: AsyncClient, stub: StubQueries
) -> None:
    response = await client.get(PUBLIC)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "size": 24}
    assert stub.filters == [
        PublicPerfumeFilters(
            q=None,
            brands=(),
            families=(),
            genders=(),
            min_price_cents=None,
            max_price_cents=None,
            sort="name",
            page=1,
            size=24,
        )
    ]


async def test_brand_family_and_gender_are_repeatable(
    client: AsyncClient, stub: StubQueries
) -> None:
    response = await client.get(
        PUBLIC,
        params=[
            ("brand", "versace"),
            ("brand", "lancome"),
            ("family", "woody"),
            ("family", "floral"),
            ("gender", "men"),
            ("gender", "unisex"),
        ],
    )

    assert response.status_code == 200
    [filters] = stub.filters
    assert filters.brands == ("versace", "lancome")
    assert filters.families == ("woody", "floral")
    assert filters.genders == ("men", "unisex")


async def test_every_other_parameter_is_parsed(client: AsyncClient, stub: StubQueries) -> None:
    response = await client.get(
        PUBLIC,
        params={
            "q": "  lanc ",
            "min_price_cents": 100,
            "max_price_cents": 900,
            "sort": "price_desc",
            "page": 3,
            "size": 48,
        },
    )

    assert response.status_code == 200
    [filters] = stub.filters
    assert filters.q == "lanc"  # trimmed by the use case
    assert (filters.min_price_cents, filters.max_price_cents) == (100, 900)
    assert (filters.sort, filters.page, filters.size) == ("price_desc", 3, 48)
    assert response.json()["page"] == 3


@pytest.mark.parametrize("sort", ["name", "price_asc", "price_desc", "newest"])
async def test_every_documented_sort_is_accepted(
    client: AsyncClient, stub: StubQueries, sort: str
) -> None:
    assert (await client.get(PUBLIC, params={"sort": sort})).status_code == 200
    assert stub.filters[0].sort == sort


@pytest.mark.parametrize(
    "params",
    [
        {"size": 49},
        {"size": 0},
        {"page": 0},
        {"min_price_cents": -1},
        {"max_price_cents": -1},
        {"min_price_cents": "cheap"},
        {"sort": "popularity"},
        {"gender": "robot"},
        {"q": "x" * 101},
    ],
)
async def test_invalid_query_parameters_are_a_validation_error(
    client: AsyncClient, stub: StubQueries, params: dict[str, Any]
) -> None:
    response = await client.get(PUBLIC, params=params)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert stub.filters == []


async def test_the_limits_themselves_are_valid(client: AsyncClient, stub: StubQueries) -> None:
    response = await client.get(
        PUBLIC, params={"size": 1, "page": 1, "min_price_cents": 0, "q": "x" * 100}
    )

    assert response.status_code == 200


async def test_a_blank_search_reaches_the_query_as_no_search(
    client: AsyncClient, stub: StubQueries
) -> None:
    await client.get(PUBLIC, params={"q": "   "})

    assert stub.filters[0].q is None


# --- GET /perfumes: shape ---------------------------------------------------------------------


async def test_the_list_answers_cards_with_refs_and_the_from_price(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    add_presentation(perfume, 100, 250_000)
    unwrap_ok(perfume.publish(NOW))
    world.perfumes.by_id[perfume.id] = perfume

    response = await client.get(PUBLIC)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "slug": "versace-eros-edt",
                "name": "Eros",
                "gender": "men",
                "brand": {"name": "Versace", "slug": "versace"},
                "concentration": {"name": "Eau de Toilette", "abbreviation": "EDT"},
                "family": {"name": "Aromática", "slug": "aromatica"},
                "price_from_cents": 250_000,
                "on_sale": False,
            }
        ],
        "total": 1,
        "page": 1,
        "size": 24,
    }


# --- GET /perfumes/{slug} ---------------------------------------------------------------------


async def test_the_detail_answers_active_presentations_with_the_effective_price(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    perfume.add_presentation(
        Ml(100),
        Price(Money(390_000)),
        Sale(Money(350_000), NOW, NOW + timedelta(days=2)),
        Availability("made_to_order", 7, 10),
        created_at=NOW,
    )
    add_presentation(perfume, 50, 250_000)
    unwrap_ok(perfume.publish(NOW))
    world.perfumes.by_id[perfume.id] = perfume

    response = await client.get(f"{PUBLIC}/{perfume.slug}")

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "versace-eros-edt"
    assert [p["ml"] for p in body["presentations"]] == [50, 100]
    plain, on_sale = body["presentations"]
    assert plain["price_cents"] == 250_000
    assert (plain["regular_price_cents"], plain["sale_ends_at"]) == (None, None)
    assert on_sale["price_cents"] == 350_000
    assert on_sale["regular_price_cents"] == 390_000
    assert on_sale["sale_ends_at"].startswith("2026-10-04T12:00:00")
    assert (on_sale["availability"], on_sale["lead_time_min_days"]) == ("made_to_order", 7)
    assert set(body) == {
        "slug",
        "name",
        "gender",
        "description",
        "brand",
        "concentration",
        "family",
        "top_notes",
        "heart_notes",
        "base_notes",
        "presentations",
    }


async def test_an_unknown_slug_is_a_404_with_the_catalog_code(client: AsyncClient) -> None:
    response = await client.get(f"{PUBLIC}/nothing-here")

    assert response.status_code == 404
    assert response.json()["code"] == "CATALOG_PERFUME_NOT_FOUND"


async def test_a_hidden_perfume_is_a_404(world_client: tuple[AsyncClient, World]) -> None:
    client, world = world_client
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    add_presentation(perfume)

    response = await client.get(f"{PUBLIC}/{perfume.slug}")

    assert (response.status_code, response.json()["code"]) == (404, "CATALOG_PERFUME_NOT_FOUND")


async def test_a_slug_longer_than_240_characters_is_a_validation_error(
    client: AsyncClient,
) -> None:
    assert (await client.get(f"{PUBLIC}/{'a' * 241}")).status_code == 422
    assert (await client.get(f"{PUBLIC}/{'a' * 240}")).status_code == 404


# --- Plan 005: the page number is bounded (MAX_PAGE = 10_000) ------------------------------


async def test_the_last_allowed_page_is_accepted_and_reaches_the_query(
    client: AsyncClient, stub: StubQueries
) -> None:
    response = await client.get(PUBLIC, params={"page": 10_000})

    assert response.status_code == 200
    assert response.json()["page"] == 10_000
    assert [f.page for f in stub.filters] == [10_000]


@pytest.mark.parametrize("page", [10_001, 10**18])
async def test_a_page_past_the_bound_is_a_422_and_the_query_never_runs(
    client: AsyncClient, stub: StubQueries, page: int
) -> None:
    response = await client.get(PUBLIC, params={"page": page})

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")
    assert stub.filters == []
