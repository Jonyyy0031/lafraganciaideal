from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.inventory.application.queries.stock import (
    GetInventorySettings,
    GetStock,
    ListMovements,
    ListStock,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.infrastructure.in_memory import InMemoryPresentationCatalog
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.inventory.support import (
    MADE_TO_ORDER,
    STOCKED,
    STOCKED_2,
    UNKNOWN,
    Inventory,
    presentation,
)


async def restock(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def test_get_stock_of_a_never_counted_presentation_shows_zero_and_low() -> None:
    inv = Inventory()

    result = await GetStock(inv.catalog, inv.store).execute(STOCKED)

    assert isinstance(result, Ok)
    stock = result.value
    assert stock.label == "Versace Eros EDT 50 ml"
    assert (stock.on_hand, stock.threshold_override, stock.effective_threshold, stock.is_low) == (
        0,
        None,
        3,
        True,
    )


async def test_get_stock_uses_the_override_and_flags_low_at_equality() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 20)
    await inv.set_threshold.execute(STOCKED, 25)

    result = await GetStock(inv.catalog, inv.store).execute(STOCKED)

    assert isinstance(result, Ok)
    assert (result.value.effective_threshold, result.value.is_low) == (25, True)

    await inv.set_threshold.execute(STOCKED, 20)
    equal = await GetStock(inv.catalog, inv.store).execute(STOCKED)
    assert isinstance(equal, Ok)
    assert equal.value.is_low is True

    await inv.set_threshold.execute(STOCKED, None)
    cleared = await GetStock(inv.catalog, inv.store).execute(STOCKED)
    assert isinstance(cleared, Ok)
    assert (cleared.value.effective_threshold, cleared.value.is_low) == (3, False)


async def test_get_stock_refuses_unknown_and_made_to_order() -> None:
    inv = Inventory()
    query = GetStock(inv.catalog, inv.store)

    unknown = await query.execute(UNKNOWN)
    made_to_order = await query.execute(MADE_TO_ORDER)

    assert isinstance(unknown, Err)
    assert unknown.error.code == "INVENTORY_PRESENTATION_NOT_FOUND"
    assert isinstance(made_to_order, Err)
    assert made_to_order.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"


async def test_list_movements_is_newest_first_and_paginated() -> None:
    inv = Inventory()
    for quantity in (1, 2, 3):
        await restock(inv, STOCKED, quantity)
        inv.clock.current += timedelta(minutes=1)
    query = ListMovements(inv.catalog, inv.store)

    first = await query.execute(STOCKED, page=1, size=2)
    second = await query.execute(STOCKED, page=2, size=2)

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert [m.quantity for m in first.value.items] == [3, 2]
    assert [m.quantity for m in second.value.items] == [1]
    assert (first.value.total, first.value.page, first.value.size) == (3, 1, 2)


async def test_list_movements_refuses_an_unknown_presentation() -> None:
    inv = Inventory()

    result = await ListMovements(inv.catalog, inv.store).execute(UNKNOWN, page=1, size=10)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_FOUND"


async def test_list_stock_includes_never_counted_presentations_with_zero() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)

    page = await ListStock(inv.catalog, inv.store).execute(low=False, page=1, size=10)

    assert [(line.label, line.on_hand, line.is_low) for line in page.items] == [
        ("Versace Eros EDT 50 ml", 10, False),
        ("Versace Eros EDT 100 ml", 0, True),
    ]
    assert page.total == 2


async def test_list_stock_excludes_made_to_order_presentations() -> None:
    inv = Inventory()

    page = await ListStock(inv.catalog, inv.store).execute(low=False, page=1, size=10)

    assert MADE_TO_ORDER not in {line.presentation_id for line in page.items}


async def test_list_stock_low_filter_keeps_only_low_ones_and_respects_overrides() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    await restock(inv, STOCKED_2, 3)  # == global threshold: low
    query = ListStock(inv.catalog, inv.store)

    low = await query.execute(low=True, page=1, size=10)
    assert [line.presentation_id for line in low.items] == [STOCKED_2]

    await inv.set_threshold.execute(STOCKED, 10)  # override makes 10 low
    low = await query.execute(low=True, page=1, size=10)
    assert [line.presentation_id for line in low.items] == [STOCKED, STOCKED_2]
    assert low.items[0].effective_threshold == 10


async def test_list_stock_paginates_after_filtering_and_reports_the_filtered_total() -> None:
    inv = Inventory()
    query = ListStock(inv.catalog, inv.store)
    page_one = await query.execute(low=True, page=1, size=1)
    page_two = await query.execute(low=True, page=2, size=1)
    beyond = await query.execute(low=True, page=3, size=1)

    assert [line.presentation_id for line in page_one.items] == [STOCKED]
    assert [line.presentation_id for line in page_two.items] == [STOCKED_2]
    assert beyond.items == []
    assert (page_one.total, page_two.total, beyond.total) == (2, 2, 2)


async def test_list_stock_follows_the_catalog_order() -> None:
    catalog = InMemoryPresentationCatalog(
        presentation(UUID(int=2), "B"), presentation(UUID(int=1), "A")
    )
    inv = Inventory(catalog)

    page = await ListStock(inv.catalog, inv.store).execute(low=False, page=1, size=10)

    assert [line.label for line in page.items] == ["B", "A"]


async def test_get_inventory_settings_defaults_to_three_then_follows_updates() -> None:
    inv = Inventory()
    query = GetInventorySettings(inv.store)

    assert (await query.execute()).low_stock_threshold == 3
    await inv.update_settings.execute(7)
    assert (await query.execute()).low_stock_threshold == 7
