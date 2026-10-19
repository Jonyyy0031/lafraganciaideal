"""Plan 004 step 1, application: `ReturnReservation` and the commit/release refusals of a
returned reservation (deviation 2), against the in-memory adapters."""

from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.reservation import ReservationStatus
from fragancia_api.modules.inventory.domain.stock import MovementKind, StockChanged
from fragancia_api.shared.kernel import DomainError, Err, Ok
from tests.unit.inventory.support import STOCKED, STOCKED_2, UNKNOWN, Inventory


async def restock(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def reserve(inv: Inventory, quantities: dict[UUID, int], reference: str = "order:1") -> UUID:
    result = await inv.reserve.execute(
        reference, quantities, inv.clock.now() + timedelta(minutes=2)
    )
    assert isinstance(result, Ok)
    return result.value


def refusal(result: object) -> DomainError:
    assert isinstance(result, Err)
    error: DomainError = result.error
    return error


def unit_state(inv: Inventory, presentation_id: UUID) -> tuple[int, int]:
    item = inv.store.items[presentation_id]
    return item.on_hand, item.reserved


async def sold(inv: Inventory, quantities: dict[UUID, int], reference: str = "order:1") -> UUID:
    reservation_id = await reserve(inv, quantities, reference)
    assert isinstance(await inv.commit.execute(reservation_id), Ok)
    return reservation_id


# --- ReturnReservation -------------------------------------------------------------------------


async def test_return_puts_the_sold_units_back_with_one_return_movement() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2}, "order:42")
    assert unit_state(inv, STOCKED) == (3, 0)

    result = await inv.return_sold.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (5, 0)
    movement = inv.store.movements_log[-1]
    assert (movement.kind, movement.delta, movement.quantity, movement.on_hand_after) == (
        MovementKind.RETURN,
        2,
        2,
        5,
    )
    assert (movement.reason.value, movement.actor_id) == ("Devolución: order:42", "system")
    assert movement.created_at == inv.clock.now()
    stored = inv.store.reservations.by_id[reservation_id]
    assert (stored.status, stored.closed_at) == (ReservationStatus.RETURNED, inv.clock.now())


async def test_return_writes_one_movement_per_presentation() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 5)
    reservation_id = await sold(inv, {STOCKED: 2, STOCKED_2: 3})
    before = len(inv.store.movements_log)

    assert isinstance(await inv.return_sold.execute(reservation_id), Ok)

    new = inv.store.movements_log[before:]
    assert sorted((m.kind.value, m.quantity) for m in new) == [("return", 2), ("return", 3)]
    assert unit_state(inv, STOCKED) == (5, 0)
    assert unit_state(inv, STOCKED_2) == (5, 0)


async def test_return_publishes_stock_changed_per_item() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2})
    inv.events.published.clear()

    await inv.return_sold.execute(reservation_id)

    changed = [e for e in inv.events.published if isinstance(e, StockChanged)]
    assert [(e.presentation_id, e.available) for e in changed] == [(STOCKED, 5)]


async def test_returning_twice_is_a_no_op_ok() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2})
    await inv.return_sold.execute(reservation_id)
    movements = len(inv.store.movements_log)
    inv.events.published.clear()

    result = await inv.return_sold.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (5, 0)
    assert len(inv.store.movements_log) == movements
    assert inv.events.published == []


async def test_return_of_an_active_reservation_is_refused_and_changes_nothing() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})

    error = refusal(await inv.return_sold.execute(reservation_id))

    assert error.code == "INVENTORY_RESERVATION_NOT_COMMITTED"
    assert unit_state(inv, STOCKED) == (5, 2)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.ACTIVE


async def test_return_of_a_released_reservation_is_refused() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await inv.release.execute(reservation_id)

    error = refusal(await inv.return_sold.execute(reservation_id))

    assert error.code == "INVENTORY_RESERVATION_NOT_COMMITTED"
    assert unit_state(inv, STOCKED) == (5, 0)


async def test_return_of_an_expired_reservation_is_refused() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.clock.current += timedelta(minutes=3)
    assert await inv.expire.run_once() == 1

    error = refusal(await inv.return_sold.execute(reservation_id))

    assert error.code == "INVENTORY_RESERVATION_NOT_COMMITTED"
    assert unit_state(inv, STOCKED) == (5, 0)


async def test_return_an_unknown_reservation_is_not_found() -> None:
    inv = Inventory()

    error = refusal(await inv.return_sold.execute(UNKNOWN))

    assert error.code == "INVENTORY_RESERVATION_NOT_FOUND"


async def test_the_facade_returns_sold_units_and_passes_a_refusal_through() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2})

    assert isinstance(await inv.facade.return_sold(reservation_id), Ok)
    refused = await inv.facade.return_sold(UNKNOWN)

    assert unit_state(inv, STOCKED) == (5, 0)
    assert refusal(refused).code == "INVENTORY_RESERVATION_NOT_FOUND"


# --- deviation 2: a returned reservation can be neither committed nor released -----------------


async def test_commit_of_a_returned_reservation_is_refused_and_sells_nothing_again() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2})
    await inv.return_sold.execute(reservation_id)
    movements = len(inv.store.movements_log)

    error = refusal(await inv.commit.execute(reservation_id))

    assert error.code == "INVENTORY_RESERVATION_NOT_ACTIVE"
    assert unit_state(inv, STOCKED) == (5, 0)
    assert len(inv.store.movements_log) == movements
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.RETURNED


async def test_release_of_a_returned_reservation_is_refused_as_already_sold() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2})
    await inv.return_sold.execute(reservation_id)

    error = refusal(await inv.release.execute(reservation_id))

    assert error.code == "INVENTORY_RESERVATION_COMMITTED"
    assert unit_state(inv, STOCKED) == (5, 0)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.RETURNED


async def test_the_facade_refuses_commit_and_release_of_a_returned_reservation() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await sold(inv, {STOCKED: 2})
    await inv.facade.return_sold(reservation_id)

    committed = await inv.facade.commit(reservation_id)
    released = await inv.facade.release(reservation_id)

    assert refusal(committed).code == "INVENTORY_RESERVATION_NOT_ACTIVE"
    assert refusal(released).code == "INVENTORY_RESERVATION_COMMITTED"
