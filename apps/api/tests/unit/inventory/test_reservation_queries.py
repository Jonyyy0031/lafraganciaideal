"""Plan 003, queries: `reserved` and `available` in `GetStock`/`ListStock`, the low flag by
available, and `ListReservationHolds`."""

from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.inventory.application.queries.stock import (
    GetStock,
    ListReservationHolds,
    ListStock,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.inventory.support import MADE_TO_ORDER, STOCKED, STOCKED_2, UNKNOWN, Inventory


async def restock(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def reserve(
    inv: Inventory, quantities: dict[UUID, int], reference: str, minutes: int = 2
) -> UUID:
    result = await inv.reserve.execute(
        reference, quantities, inv.clock.now() + timedelta(minutes=minutes)
    )
    assert isinstance(result, Ok)
    return result.value


# --- GetStock / ListStock ----------------------------------------------------------------------


async def test_get_stock_shows_on_hand_reserved_and_available() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await reserve(inv, {STOCKED: 2}, "QA-1")

    result = await GetStock(inv.catalog, inv.store).execute(STOCKED)

    assert isinstance(result, Ok)
    stock = result.value
    assert (stock.on_hand, stock.reserved, stock.available) == (5, 2, 3)
    assert (stock.effective_threshold, stock.is_low) == (3, True)  # 3 <= 3


async def test_get_stock_of_a_never_counted_presentation_has_nothing_reserved() -> None:
    inv = Inventory()

    result = await GetStock(inv.catalog, inv.store).execute(STOCKED)

    assert isinstance(result, Ok)
    assert (result.value.on_hand, result.value.reserved, result.value.available) == (0, 0, 0)


async def test_get_stock_floors_available_at_zero_when_a_loss_left_less_than_reserved() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 3)
    await reserve(inv, {STOCKED: 2}, "QA-1")
    await inv.record.execute(
        STOCKED, MovementRequest(kind="loss", quantity=2, reason="Rotura"), "a"
    )

    result = await GetStock(inv.catalog, inv.store).execute(STOCKED)

    assert isinstance(result, Ok)
    assert (result.value.on_hand, result.value.reserved, result.value.available) == (1, 2, 0)
    assert result.value.is_low is True


async def test_a_reservation_flips_the_low_flag_although_on_hand_is_above_the_threshold() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    query = GetStock(inv.catalog, inv.store)
    before = await query.execute(STOCKED)
    assert isinstance(before, Ok)
    assert before.value.is_low is False

    await reserve(inv, {STOCKED: 7}, "QA-1")

    after = await query.execute(STOCKED)
    assert isinstance(after, Ok)
    assert (after.value.on_hand, after.value.available, after.value.is_low) == (10, 3, True)


async def test_releasing_clears_the_low_flag_again() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    reservation_id = await reserve(inv, {STOCKED: 8}, "QA-1")
    await inv.release.execute(reservation_id)

    result = await GetStock(inv.catalog, inv.store).execute(STOCKED)

    assert isinstance(result, Ok)
    assert (result.value.reserved, result.value.available, result.value.is_low) == (0, 10, False)


async def test_list_stock_shows_reserved_and_available_per_line() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    await restock(inv, STOCKED_2, 5)
    await reserve(inv, {STOCKED: 4, STOCKED_2: 5}, "QA-1")

    page = await ListStock(inv.catalog, inv.store).execute(low=False, page=1, size=10)

    assert [
        (line.presentation_id, line.on_hand, line.reserved, line.available) for line in page.items
    ] == [
        (STOCKED, 10, 4, 6),
        (STOCKED_2, 5, 5, 0),
    ]
    assert [line.is_low for line in page.items] == [False, True]


async def test_list_stock_low_filter_uses_available() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    await restock(inv, STOCKED_2, 10)
    await reserve(inv, {STOCKED_2: 8}, "QA-1")  # on hand 10 but available 2

    page = await ListStock(inv.catalog, inv.store).execute(low=True, page=1, size=10)

    assert [line.presentation_id for line in page.items] == [STOCKED_2]
    assert page.total == 1


# --- ListReservationHolds ----------------------------------------------------------------------


async def test_holds_list_the_active_reservations_soonest_expiry_first() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 20)
    await restock(inv, STOCKED_2, 20)
    late = await reserve(inv, {STOCKED: 1}, "QA-LATE", minutes=60)
    soon = await reserve(inv, {STOCKED: 2, STOCKED_2: 9}, "QA-SOON", minutes=5)
    await reserve(inv, {STOCKED_2: 3}, "QA-OTHER", minutes=1)

    result = await ListReservationHolds(inv.catalog, inv.store).execute(STOCKED, page=1, size=10)

    assert isinstance(result, Ok)
    page = result.value
    assert [(h.reservation_id, h.reference, h.quantity) for h in page.items] == [
        (soon, "QA-SOON", 2),
        (late, "QA-LATE", 1),
    ]
    assert page.items[0].expires_at == inv.clock.now() + timedelta(minutes=5)
    assert page.items[0].created_at == inv.clock.now()
    assert page.total == 2


async def test_holds_leave_out_reservations_that_are_not_active() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 20)
    kept = await reserve(inv, {STOCKED: 1}, "QA-KEPT")
    for reference, close in (("QA-C", "commit"), ("QA-R", "release")):
        closing = await reserve(inv, {STOCKED: 1}, reference)
        await getattr(inv, close).execute(closing)
    await reserve(inv, {STOCKED: 1}, "QA-E", minutes=1)
    inv.clock.current += timedelta(minutes=1, seconds=30)
    await inv.expire.run_once()

    result = await ListReservationHolds(inv.catalog, inv.store).execute(STOCKED, page=1, size=10)

    assert isinstance(result, Ok)
    assert [h.reservation_id for h in result.value.items] == [kept]


async def test_holds_paginate() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 20)
    ids = [await reserve(inv, {STOCKED: 1}, f"QA-{n}", minutes=n + 1) for n in range(3)]
    query = ListReservationHolds(inv.catalog, inv.store)

    first = await query.execute(STOCKED, page=1, size=2)
    second = await query.execute(STOCKED, page=2, size=2)

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert [h.reservation_id for h in first.value.items] == ids[:2]
    assert [h.reservation_id for h in second.value.items] == ids[2:]
    assert (first.value.total, second.value.total) == (3, 3)


async def test_holds_of_an_unknown_presentation_are_not_found() -> None:
    inv = Inventory()

    result = await ListReservationHolds(inv.catalog, inv.store).execute(UNKNOWN, page=1, size=10)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_FOUND"


async def test_holds_of_a_known_presentation_without_reservations_are_empty() -> None:
    inv = Inventory()

    result = await ListReservationHolds(inv.catalog, inv.store).execute(STOCKED, page=1, size=10)

    assert isinstance(result, Ok)
    assert (result.value.items, result.value.total) == ([], 0)


async def test_holds_of_a_made_to_order_presentation_are_empty_not_an_error() -> None:
    """The query only checks the presentation exists (as `ListMovements` does)."""
    inv = Inventory()

    result = await ListReservationHolds(inv.catalog, inv.store).execute(
        MADE_TO_ORDER, page=1, size=10
    )

    assert isinstance(result, Ok)
    assert result.value.total == 0
