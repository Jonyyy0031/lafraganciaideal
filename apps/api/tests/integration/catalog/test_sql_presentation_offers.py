"""The `PresentationOffers` facade's SQL adapter against `fragancia_test`: sale window,
visibility, photo URL, stock copy, made to order."""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import insert, update

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationOffers
from fragancia_api.modules.catalog.infrastructure.sql_presentation_offers import (
    SqlPresentationOffers,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    perfume_photos,
    perfumes,
    presentations,
)
from fragancia_api.shared.kernel import new_id
from tests.integration.catalog.presentation_rows import CREATED, CatalogRows

pytestmark = pytest.mark.integration

MEDIA = "http://media.test"
NOW = CREATED + timedelta(days=10)
MADE_TO_ORDER = {"availability": "made_to_order", "lead_time_min_days": 3, "lead_time_max_days": 5}


def facade(container: Container) -> PresentationOffers:
    return PresentationOffers(SqlPresentationOffers(container.database, media_base_url=MEDIA))


async def add_photo(container: Container, perfume_id: UUID, position: int, key: str) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(perfume_photos).values(
                id=new_id(),
                perfume_id=perfume_id,
                position=position,
                card_key=f"{key}-card.webp",
                detail_key=f"{key}-detail.webp",
                width=800,
                height=800,
                created_at=CREATED,
            )
        )


async def test_an_in_stock_offer_carries_label_slug_prices_and_the_stock_copy(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume(
        "Eros", brand="Zz Versace", sizes=({"stock_available": 7},)
    )

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert offer.presentation_id == presentation_id
    assert offer.perfume_id == perfume_id
    assert offer.perfume_slug == f"eros-{perfume_id}"
    assert offer.label == "Zz Versace Eros ZRE 100 ml"
    assert offer.ml == 100
    assert offer.card_url is None
    assert (offer.price_cents, offer.regular_price_cents) == (250_000, 250_000)
    assert (offer.availability, offer.lead_time_min_days, offer.lead_time_max_days) == (
        "in_stock",
        None,
        None,
    )
    assert offer.is_visible is True
    assert offer.available_units == 7


async def test_a_presentation_never_counted_reads_zero_units(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert offer.available_units == 0


async def test_a_made_to_order_offer_has_lead_times_and_no_units_even_with_a_stock_copy(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(
        sizes=({**MADE_TO_ORDER, "stock_available": 9},)
    )

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert (offer.availability, offer.lead_time_min_days, offer.lead_time_max_days) == (
        "made_to_order",
        3,
        5,
    )
    assert offer.available_units is None


async def test_the_price_is_the_sale_price_inside_the_window_and_regular_outside(
    container: Container, catalog_rows: CatalogRows
) -> None:
    start, end = NOW, NOW + timedelta(days=2)
    _, [presentation_id] = await catalog_rows.perfume(
        sizes=({"sale_price_cents": 200_000, "sale_starts_at": start, "sale_ends_at": end},)
    )
    offers = facade(container)

    prices = [
        (await offers.offers([presentation_id], moment))[presentation_id].price_cents
        for moment in (start - timedelta(seconds=1), start, start + timedelta(days=1), end)
    ]

    assert prices == [250_000, 200_000, 200_000, 250_000]
    regular = (await offers.offers([presentation_id], start))[presentation_id].regular_price_cents
    assert regular == 250_000


async def test_a_sale_without_dates_applies_always(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(sizes=({"sale_price_cents": 200_000},))

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert (offer.price_cents, offer.regular_price_cents) == (200_000, 250_000)


async def test_the_card_url_is_the_first_photo_by_position(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await add_photo(container, perfume_id, 2, "second")
    await add_photo(container, perfume_id, 1, "first")

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert offer.card_url == f"{MEDIA}/first-card.webp"


async def test_unknown_ids_are_absent_and_an_empty_list_is_empty(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [known] = await catalog_rows.perfume()

    found = await facade(container).offers([known, new_id()], NOW)

    assert list(found) == [known]
    assert await facade(container).offers([], NOW) == {}


async def test_several_presentations_of_several_perfumes_come_back_by_id(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [a1, a2] = await catalog_rows.perfume("Aaa", sizes=({}, {"stock_available": 4}))
    _, [b1] = await catalog_rows.perfume("Bbb")

    found = await facade(container).offers([a1, a2, b1], NOW)

    assert set(found) == {a1, a2, b1}
    assert (found[a1].ml, found[a2].ml) == (100, 101)
    assert found[a2].available_units == 4


@pytest.mark.parametrize(
    ("perfume_kwargs", "size"),
    [
        ({}, {"is_active": False}),
        ({"archived": True}, {}),
        ({"brand_active": False}, {}),
    ],
    ids=["inactive presentation", "archived perfume", "inactive brand"],
)
async def test_an_offer_that_cannot_be_sold_is_returned_but_not_visible(
    container: Container,
    catalog_rows: CatalogRows,
    perfume_kwargs: dict[str, object],
    size: dict[str, object],
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(sizes=(size,), **perfume_kwargs)  # type: ignore[arg-type]

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert offer.is_visible is False


async def test_an_unpublished_perfume_is_not_visible(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            update(perfumes).where(perfumes.c.id == perfume_id).values(is_published=False)
        )

    offer = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert offer.is_visible is False


async def test_the_offer_follows_the_catalog_when_rows_change(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(sizes=({"stock_available": 2},))
    before = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    async with container.database.engine.begin() as connection:
        await connection.execute(
            update(presentations)
            .where(presentations.c.id == presentation_id)
            .values(price_cents=300_000, stock_available=0)
        )
    after = (await facade(container).offers([presentation_id], NOW))[presentation_id]

    assert (before.price_cents, before.available_units) == (250_000, 2)
    assert (after.price_cents, after.available_units) == (300_000, 0)
