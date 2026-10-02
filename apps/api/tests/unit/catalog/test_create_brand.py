from datetime import UTC, datetime

import pytest

from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.domain.brand import Brand, BrandCreated, BrandName
from fragancia_api.modules.catalog.infrastructure.in_memory import InMemoryBrands
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from fragancia_api.shared.kernel import Err, Ok


@pytest.fixture
def brands() -> InMemoryBrands:
    return InMemoryBrands()


@pytest.fixture
def events() -> RecordingEventPublisher:
    return RecordingEventPublisher()


@pytest.fixture
def create_brand(brands: InMemoryBrands, events: RecordingEventPublisher) -> CreateBrand:
    return CreateBrand(
        brands=brands,
        transactions=InMemoryTransactionRunner(),
        events=events,
        clock=FixedClock(),
    )


async def test_creates_an_active_brand_and_publishes_the_event(
    create_brand: CreateBrand, brands: InMemoryBrands, events: RecordingEventPublisher
) -> None:
    result = await create_brand.execute("  Maison   Margiela ")

    assert isinstance(result, Ok)
    brand = brands.by_id[result.value]
    assert (brand.name.value, brand.slug, brand.is_active) == (
        "Maison Margiela",
        "maison-margiela",
        True,
    )
    assert brand.created_at == FixedClock().now()
    assert [type(e) for e in events.published] == [BrandCreated]


async def test_a_name_with_the_same_slug_is_a_conflict(
    create_brand: CreateBrand, events: RecordingEventPublisher
) -> None:
    await create_brand.execute("Maison Margiela")
    result = await create_brand.execute("maison margiéla")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"
    assert len(events.published) == 1  # only the first brand


async def test_an_invalid_name_creates_and_publishes_nothing(
    create_brand: CreateBrand, brands: InMemoryBrands, events: RecordingEventPublisher
) -> None:
    result = await create_brand.execute("x")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_NAME_INVALID"
    assert brands.by_id == {}
    assert events.published == []


def _brand(raw: str, *, active: bool = True) -> Brand:
    result = BrandName.create(raw)
    assert isinstance(result, Ok)
    brand = Brand.create(result.value, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    brand.is_active = active
    return brand


async def test_public_list_has_active_brands_ordered_by_name() -> None:
    store = InMemoryBrands(_brand("Zara"), _brand("armani"), _brand("Lancôme", active=False))

    listed = await ListPublicBrands(store).execute()

    assert [b.name for b in listed] == ["armani", "Zara"]


async def test_admin_list_is_paginated_and_includes_inactive_brands() -> None:
    store = InMemoryBrands(_brand("C brand"), _brand("A brand"), _brand("B brand", active=False))

    page = await ListAdminBrands(store).execute(page=2, size=2)

    assert (page.total, page.page, page.size) == (3, 2, 2)
    assert [b.name for b in page.items] == ["C brand"]
