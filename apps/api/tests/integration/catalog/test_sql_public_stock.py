"""Plan 002: the public SQL with the stock copy -- buyable-only `price_from`, the price filters
and sort on it, `sold_out`, `in_stock=true` and the detail's `stock_status`."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.contracts import PublicPerfumeCard
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.infrastructure.sql_perfume_queries import SqlPerfumeQueries
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
NO_FILTERS = PublicPerfumeFilters(
    q=None,
    brands=(),
    families=(),
    genders=(),
    min_price_cents=None,
    max_price_cents=None,
    sort="name",
    page=1,
    size=48,
)
MADE_TO_ORDER = {"availability": "made_to_order", "lead_time_min_days": 7, "lead_time_max_days": 10}


def size(ml: int, price: int, stock: int, **more: Any) -> dict[str, Any]:
    return {"ml": ml, "price_cents": price, "stock_available": stock, **more}


def queries(container: Container) -> SqlPerfumeQueries:
    return SqlPerfumeQueries(container.database, media_base_url="http://media.test")


async def cards(container: Container, **filters: Any) -> dict[str, PublicPerfumeCard]:
    page = await queries(container).list_public(replace(NO_FILTERS, **filters), now=NOW)
    return {card.name: card for card in page.items}


async def names(container: Container, **filters: Any) -> list[str]:
    page = await queries(container).list_public(replace(NO_FILTERS, **filters), now=NOW)
    return [card.name for card in page.items]


@pytest.fixture
async def shelf(catalog_rows: CatalogRows) -> None:
    """Four perfumes; the comments say what each one's "from" price is and why."""
    # Alfa: cheapest overall is a sold-out 100_000; the buyable minimum is 250_000.
    await catalog_rows.perfume("Alfa", sizes=(size(50, 100_000, 0), size(100, 250_000, 4)))
    # Beta: everything sold out: falls back to the cheapest overall, 120_000.
    await catalog_rows.perfume("Beta", sizes=(size(50, 120_000, 0), size(100, 300_000, 0)))
    # Gamma: a buyable 180_000.
    await catalog_rows.perfume("Gamma", sizes=(size(100, 180_000, 2),))
    # Delta: made to order only (never counted): buyable at 400_000.
    await catalog_rows.perfume("Delta", sizes=(size(100, 400_000, 0, **MADE_TO_ORDER),))


# --- price_from and sold_out on the card -------------------------------------------------------


@pytest.mark.usefixtures("shelf")
async def test_price_from_counts_only_buyable_presentations(container: Container) -> None:
    found = await cards(container)

    assert found["Alfa"].price_from_cents == 250_000  # not the sold-out 100_000
    assert found["Gamma"].price_from_cents == 180_000
    assert found["Delta"].price_from_cents == 400_000


@pytest.mark.usefixtures("shelf")
async def test_price_from_falls_back_to_the_lowest_price_when_nothing_is_buyable(
    container: Container,
) -> None:
    found = await cards(container)

    assert found["Beta"].price_from_cents == 120_000
    assert found["Beta"].sold_out is True


@pytest.mark.usefixtures("shelf")
async def test_sold_out_is_true_only_when_nothing_can_be_bought(container: Container) -> None:
    found = await cards(container)

    assert {name: card.sold_out for name, card in found.items()} == {
        "Alfa": False,  # one size is sold out, another is not
        "Beta": True,
        "Gamma": False,
        "Delta": False,  # made to order: buyable with no count
    }


async def test_one_unit_is_buyable_and_zero_is_not(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await catalog_rows.perfume("One", sizes=(size(100, 100_000, 1),))
    await catalog_rows.perfume("Zero", sizes=(size(100, 100_000, 0),))

    found = await cards(container)

    assert (found["One"].sold_out, found["Zero"].sold_out) == (False, True)


async def test_an_inactive_presentation_with_units_does_not_make_the_perfume_buyable(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await catalog_rows.perfume(
        "Ghost", sizes=(size(50, 100_000, 0), size(100, 90_000, 9, is_active=False))
    )

    [card] = (await cards(container)).values()

    assert (card.price_from_cents, card.sold_out) == (100_000, True)


async def test_the_buyable_minimum_uses_the_effective_sale_price_and_on_sale_keeps_its_meaning(
    container: Container, catalog_rows: CatalogRows
) -> None:
    sale = {"sale_starts_at": NOW - DAY, "sale_ends_at": NOW + DAY}
    await catalog_rows.perfume(
        "Offer",
        sizes=(
            size(50, 300_000, 0, sale_price_cents=50_000, **sale),  # sold out, cheap sale
            size(100, 300_000, 5, sale_price_cents=200_000, **sale),  # buyable, on sale
            size(200, 260_000, 5),  # buyable, regular price
        ),
    )
    await catalog_rows.perfume(
        "Only sold out on sale",
        sizes=(
            size(50, 300_000, 0, sale_price_cents=50_000, **sale),
            size(100, 280_000, 5),
        ),
    )

    found = await cards(container)

    assert found["Offer"].price_from_cents == 200_000
    assert found["Offer"].on_sale is True
    # `on_sale` still looks at every active presentation, as before this plan.
    assert found["Only sold out on sale"].price_from_cents == 280_000
    assert found["Only sold out on sale"].on_sale is True


# --- price filters and sort use that value -----------------------------------------------------


@pytest.mark.usefixtures("shelf")
async def test_the_min_price_filter_uses_the_buyable_price(container: Container) -> None:
    # Alfa's cheapest size is 100_000 but sold out: its "from" price is 250_000.
    assert await names(container, min_price_cents=200_000) == ["Alfa", "Delta"]
    assert await names(container, min_price_cents=150_000, max_price_cents=200_000) == ["Gamma"]


@pytest.mark.usefixtures("shelf")
async def test_the_max_price_filter_uses_the_buyable_price(container: Container) -> None:
    assert await names(container, max_price_cents=150_000) == ["Beta"]  # Beta's fallback price
    assert await names(container, max_price_cents=260_000) == ["Alfa", "Beta", "Gamma"]


@pytest.mark.usefixtures("shelf")
async def test_the_price_sort_uses_the_buyable_price(container: Container) -> None:
    assert await names(container, sort="price_asc") == ["Beta", "Gamma", "Alfa", "Delta"]
    assert await names(container, sort="price_desc") == ["Delta", "Alfa", "Gamma", "Beta"]


# --- in_stock=true -----------------------------------------------------------------------------


@pytest.mark.usefixtures("shelf")
async def test_in_stock_hides_the_perfume_with_nothing_buyable(container: Container) -> None:
    page = await queries(container).list_public(replace(NO_FILTERS, in_stock=True), now=NOW)

    assert [card.name for card in page.items] == ["Alfa", "Delta", "Gamma"]
    assert page.total == 3
    assert not any(card.sold_out for card in page.items)


@pytest.mark.usefixtures("shelf")
async def test_without_in_stock_the_sold_out_perfume_stays_listed(container: Container) -> None:
    page = await queries(container).list_public(NO_FILTERS, now=NOW)

    assert page.total == 4
    assert "Beta" in [card.name for card in page.items]


@pytest.mark.usefixtures("shelf")
async def test_in_stock_combines_with_the_price_filter_and_sort(container: Container) -> None:
    assert await names(container, in_stock=True, max_price_cents=260_000, sort="price_desc") == [
        "Alfa",
        "Gamma",
    ]


# --- the detail --------------------------------------------------------------------------------


async def test_the_detail_status_at_every_boundary(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, _ = await catalog_rows.perfume(
        "Eros",
        sizes=(
            size(10, 100_000, 0),  # sold out
            size(20, 100_000, 1),  # last units
            size(30, 100_000, 3),  # equal to the threshold of 3
            size(40, 100_000, 4),  # one above it
            size(50, 100_000, 10, stock_threshold=15),  # override above the stock
            size(60, 100_000, 15, stock_threshold=15),  # equal to the override
            size(70, 100_000, 16, stock_threshold=15),
            size(80, 100_000, 0, stock_threshold=0),
            size(90, 100_000, 1, stock_threshold=0),
            size(100, 100_000, 0, **MADE_TO_ORDER),  # made to order: no status
            size(110, 100_000, 7, **MADE_TO_ORDER),
            size(120, 100_000, 8, is_active=False),  # inactive: not shown
        ),
    )

    perfume = await queries(container).get_public(f"{slugify('Eros')}-{perfume_id}", now=NOW)

    assert perfume is not None
    assert {p.ml: p.stock_status for p in perfume.presentations} == {
        10: "sold_out",
        20: "last_units",
        30: "last_units",
        40: "available",
        50: "last_units",
        60: "last_units",
        70: "available",
        80: "sold_out",
        90: "available",
        100: None,
        110: None,
    }


async def test_the_detail_json_never_carries_the_count(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, _ = await catalog_rows.perfume("Eros", sizes=(size(100, 100_000, 6),))

    perfume = await queries(container).get_public(f"{slugify('Eros')}-{perfume_id}", now=NOW)

    assert perfume is not None
    dumped = perfume.presentations[0].model_dump()
    assert dumped["stock_status"] == "available"
    assert not {"stock_available", "stock_threshold", "stock_as_of", "available"} & set(dumped)
