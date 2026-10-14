"""The catalog facade's SQL adapter against `fragancia_test`."""

from uuid import UUID

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory, PresentationRef
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.shared.kernel import new_id
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration


MADE_TO_ORDER = {"availability": "made_to_order", "lead_time_min_days": 3, "lead_time_max_days": 5}


def directory(container: Container) -> PresentationDirectory:
    return PresentationDirectory(SqlPresentationDirectory(container.database))


async def test_find_returns_names_ml_and_kind(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume("Eros", brand="Zz Versace")

    found = await directory(container).find([presentation_id])

    assert found == {
        presentation_id: PresentationRef(
            presentation_id=presentation_id,
            perfume_id=perfume_id,
            perfume_name="Eros",
            brand_name="Zz Versace",
            concentration_abbreviation="ZRE",
            ml=100,
            availability="in_stock",
            is_sellable=True,
        )
    }


async def test_find_omits_unknown_ids_and_accepts_an_empty_list(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [known] = await catalog_rows.perfume()

    found = await directory(container).find([known, new_id()])

    assert list(found) == [known]
    assert await directory(container).find([]) == {}


async def test_find_reports_made_to_order(container: Container, catalog_rows: CatalogRows) -> None:
    _, [presentation_id] = await catalog_rows.perfume(sizes=(MADE_TO_ORDER,))

    ref = (await directory(container).find([presentation_id]))[presentation_id]

    assert (ref.availability, ref.is_sellable) == ("made_to_order", True)


@pytest.mark.parametrize(
    ("perfume_kwargs", "size"),
    [
        ({}, {"is_active": False}),
        ({"archived": True}, {}),
        ({"brand_active": False}, {}),
    ],
    ids=["inactive presentation", "archived perfume", "inactive brand"],
)
async def test_find_still_returns_but_flags_a_presentation_that_cannot_be_sold(
    container: Container,
    catalog_rows: CatalogRows,
    perfume_kwargs: dict[str, object],
    size: dict[str, object],
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(sizes=(size,), **perfume_kwargs)  # type: ignore[arg-type]

    found = await directory(container).find([presentation_id])

    assert found[presentation_id].is_sellable is False


async def test_list_in_stock_keeps_only_active_in_stock_presentations_of_live_perfumes(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [live, made_to_order, inactive] = await catalog_rows.perfume(
        "Live", sizes=({}, MADE_TO_ORDER | {"ml": 50}, {"is_active": False, "ml": 30})
    )
    await catalog_rows.perfume("Archived", archived=True)

    listed = await directory(container).list_in_stock()

    assert [ref.presentation_id for ref in listed] == [live]
    assert made_to_order not in {ref.presentation_id for ref in listed}
    assert inactive not in {ref.presentation_id for ref in listed}


async def test_list_in_stock_includes_presentations_of_an_inactive_brand_flagged_unsellable(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(brand="Zz Closed", brand_active=False)

    listed = await directory(container).list_in_stock()

    assert [(ref.presentation_id, ref.is_sellable) for ref in listed] == [(presentation_id, False)]


async def test_list_in_stock_is_ordered_by_brand_perfume_then_ml_ignoring_case(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [b_100] = await catalog_rows.perfume("Beta", brand="zz alpha")
    _, [a_200, a_50] = await catalog_rows.perfume(
        "alpha", brand="zz alpha", sizes=({"ml": 200}, {"ml": 50})
    )
    _, [z_10] = await catalog_rows.perfume("Alpha", brand="Zz Beta", sizes=({"ml": 10},))

    listed = await directory(container).list_in_stock()

    order: list[UUID] = [ref.presentation_id for ref in listed]
    assert order == [a_50, a_200, b_100, z_10]
