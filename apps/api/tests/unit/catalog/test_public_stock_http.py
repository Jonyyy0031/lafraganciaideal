"""Plan 002: stock status on the public routes (http layer, in-memory adapters as the port)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.main.openapi import document
from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetPublicPerfume,
    ListPublicPerfumes,
)
from fragancia_api.modules.catalog.contracts import PublicPerfumePage
from fragancia_api.modules.catalog.domain.perfume import Availability, Ml, Perfume, Price
from fragancia_api.modules.catalog.http.perfume_router import perfume_routers
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from tests.support import TestActorResolver
from tests.unit.catalog.perfume_support import NOW, World, make_perfume, unwrap_ok

PUBLIC = "/api/v1/perfumes"
LATER = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class StubQueries:
    def __init__(self) -> None:
        self.filters: list[PublicPerfumeFilters] = []

    async def list_public(self, filters: PublicPerfumeFilters, *, now: datetime) -> Any:
        self.filters.append(filters)
        return PublicPerfumePage(items=[], total=0, page=filters.page, size=filters.size)

    async def get_public(self, slug: str, *, now: datetime) -> None:
        return None


def _app(queries: Any) -> Any:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    clock = FixedClock()
    services.add(ListPublicPerfumes, ListPublicPerfumes(queries, clock))
    services.add(GetPublicPerfume, GetPublicPerfume(queries, clock))
    return build_app(services, perfume_routers)


@pytest.fixture
async def stub_client() -> AsyncIterator[tuple[AsyncClient, StubQueries]]:
    stub = StubQueries()
    async with AsyncClient(transport=ASGITransport(app=_app(stub)), base_url="http://t") as c:
        yield c, stub


@pytest.fixture
async def world_client() -> AsyncIterator[tuple[AsyncClient, World]]:
    world = World()
    async with AsyncClient(
        transport=ASGITransport(app=_app(world.perfumes)), base_url="http://t"
    ) as c:
        yield c, world


def _perfume(world: World, name: str, sizes: list[tuple[int, int, str]]) -> Perfume:
    """A published perfume with `(ml, price, availability)` presentations."""
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration, name=name))
    for ml, price, availability in sizes:
        days = (7, 10) if availability == "made_to_order" else (None, None)
        unwrap_ok(
            perfume.add_presentation(
                unwrap_ok(Ml.create(ml)),
                unwrap_ok(Price.create(price)),
                None,
                unwrap_ok(Availability.create(availability, *days)),  # type: ignore[arg-type]
                created_at=NOW,
            )
        )
    unwrap_ok(perfume.publish(NOW))
    return perfume


def _id(perfume: Perfume, ml: int) -> Any:
    return next(p.id for p in perfume.presentations if p.ml.value == ml)


# --- the in_stock query parameter --------------------------------------------------------------


async def test_in_stock_defaults_to_false(stub_client: tuple[AsyncClient, StubQueries]) -> None:
    client, stub = stub_client

    await client.get(PUBLIC)

    assert stub.filters[0].in_stock is False


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
async def test_in_stock_is_parsed(
    stub_client: tuple[AsyncClient, StubQueries], raw: str, expected: bool
) -> None:
    client, stub = stub_client

    response = await client.get(PUBLIC, params={"in_stock": raw})

    assert response.status_code == 200
    assert stub.filters[0].in_stock is expected


async def test_in_stock_rejects_a_value_that_is_not_a_boolean(
    stub_client: tuple[AsyncClient, StubQueries],
) -> None:
    client, stub = stub_client

    response = await client.get(PUBLIC, params={"in_stock": "maybe"})

    assert response.status_code == 422
    assert stub.filters == []


# --- card and detail ---------------------------------------------------------------------------


async def test_the_card_is_sold_out_and_the_in_stock_filter_hides_it(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    _perfume(world, "Eros", [(100, 250_000, "in_stock")])  # never counted: 0 available

    listed = (await client.get(PUBLIC)).json()
    filtered = (await client.get(PUBLIC, params={"in_stock": "true"})).json()

    assert [(c["name"], c["sold_out"]) for c in listed["items"]] == [("Eros", True)]
    assert filtered["items"] == []


async def test_a_counted_perfume_is_not_sold_out_and_stays_under_in_stock(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    perfume = _perfume(world, "Eros", [(100, 250_000, "in_stock")])
    await world.perfumes.apply(_id(perfume, 100), 5, 3, NOW)

    filtered = (await client.get(PUBLIC, params={"in_stock": "true"})).json()

    assert [(c["name"], c["sold_out"]) for c in filtered["items"]] == [("Eros", False)]


async def test_the_detail_shows_each_status_and_never_the_count(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    perfume = _perfume(
        world,
        "Eros",
        [
            (30, 100_000, "in_stock"),
            (50, 150_000, "in_stock"),
            (75, 200_000, "in_stock"),
            (100, 250_000, "in_stock"),
            (200, 400_000, "made_to_order"),
        ],
    )
    await world.perfumes.apply(_id(perfume, 50), 10, 3, NOW)
    await world.perfumes.apply(_id(perfume, 75), 3, 3, NOW)
    # 30 ml is never counted; 100 ml was counted down to zero.
    await world.perfumes.apply(_id(perfume, 100), 0, 3, NOW)

    response = await client.get(f"{PUBLIC}/{perfume.slug}")

    assert response.status_code == 200
    statuses = {p["ml"]: p["stock_status"] for p in response.json()["presentations"]}
    assert statuses == {
        30: "sold_out",
        50: "available",
        75: "last_units",
        100: "sold_out",
        200: None,
    }
    for presentation in response.json()["presentations"]:
        assert not {"stock_available", "available", "on_hand", "stock_threshold"} & set(
            presentation
        )


async def test_an_older_event_does_not_change_the_detail(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    perfume = _perfume(world, "Eros", [(100, 250_000, "in_stock")])
    await world.perfumes.apply(_id(perfume, 100), 10, 3, LATER)
    await world.perfumes.apply(_id(perfume, 100), 0, 3, NOW)

    body = (await client.get(f"{PUBLIC}/{perfume.slug}")).json()

    assert body["presentations"][0]["stock_status"] == "available"


# --- from price, sort and filters (in-memory adapter) ------------------------------------------


async def test_the_from_price_skips_a_sold_out_size_and_falls_back_when_none_is_buyable(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    mixed = _perfume(world, "Alfa", [(50, 100_000, "in_stock"), (100, 250_000, "in_stock")])
    await world.perfumes.apply(_id(mixed, 100), 4, 3, NOW)
    _perfume(world, "Beta", [(50, 120_000, "in_stock"), (100, 300_000, "in_stock")])

    cards = {c["name"]: c for c in (await client.get(PUBLIC)).json()["items"]}

    assert (cards["Alfa"]["price_from_cents"], cards["Alfa"]["sold_out"]) == (250_000, False)
    assert (cards["Beta"]["price_from_cents"], cards["Beta"]["sold_out"]) == (120_000, True)


async def test_a_made_to_order_size_is_buyable_without_any_count(
    world_client: tuple[AsyncClient, World],
) -> None:
    client, world = world_client
    _perfume(world, "Eros", [(50, 100_000, "in_stock"), (200, 400_000, "made_to_order")])

    [card] = (await client.get(PUBLIC)).json()["items"]

    assert (card["price_from_cents"], card["sold_out"]) == (400_000, False)


# --- OpenAPI -----------------------------------------------------------------------------------


def test_the_openapi_documents_the_new_fields() -> None:
    spec = document()

    list_params = {p["name"]: p for p in spec["paths"][PUBLIC]["get"]["parameters"]}
    assert list_params["in_stock"]["schema"]["default"] is False
    schemas = spec["components"]["schemas"]
    assert "sold_out" in schemas["PublicPerfumeCard"]["required"]
    assert "stock_status" in schemas["PublicPresentation"]["required"]
    status = schemas["PublicPresentation"]["properties"]["stock_status"]
    enums = [branch.get("enum") for branch in status["anyOf"]]
    assert ["available", "last_units", "sold_out"] in enums
    assert {"type": "null"} in status["anyOf"]
