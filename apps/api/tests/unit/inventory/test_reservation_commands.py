"""Plan 003, application: the reservation commands, the expiry job and the facade, against the
in-memory adapters (the fakes keep no rollback, so these tests also prove each command validates
before it writes)."""

from datetime import timedelta
from uuid import UUID

import pytest

import fragancia_api.modules.inventory.application.commands.reservations as reservation_commands
from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.reservation import ReservationStatus
from fragancia_api.modules.inventory.domain.stock import MovementKind, StockChanged
from fragancia_api.shared.kernel import DomainError, Err, Ok
from tests.unit.inventory.support import (
    MADE_TO_ORDER,
    STOCKED,
    STOCKED_2,
    UNKNOWN,
    Inventory,
)


async def restock(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def lose(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="loss", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def reserve(
    inv: Inventory,
    quantities: dict[UUID, int],
    reference: str = "QA-1",
    minutes: int = 2,
) -> UUID:
    result = await inv.reserve.execute(
        reference, quantities, inv.clock.now() + timedelta(minutes=minutes)
    )
    assert isinstance(result, Ok)
    return result.value


def refusal(result: object) -> DomainError:
    assert isinstance(result, Err)
    error: DomainError = result.error
    return error


def events(inv: Inventory) -> list[StockChanged]:
    return [e for e in inv.events.published if isinstance(e, StockChanged)]


def unit_state(inv: Inventory, presentation_id: UUID) -> tuple[int, int]:
    item = inv.store.items[presentation_id]
    return item.on_hand, item.reserved


# --- ReserveStock ------------------------------------------------------------------------------


async def test_reserve_holds_the_units_and_returns_the_reservation_id() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)

    reservation_id = await reserve(inv, {STOCKED: 2})

    assert unit_state(inv, STOCKED) == (5, 2)
    assert inv.store.items[STOCKED].available == 3
    stored = inv.store.reservations.by_id[reservation_id]
    assert (stored.reference, stored.status) == ("QA-1", ReservationStatus.ACTIVE)
    assert stored.expires_at == inv.clock.now() + timedelta(minutes=2)
    assert [(line.presentation_id, line.quantity) for line in stored.lines] == [(STOCKED, 2)]


async def test_reserve_records_no_movement() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    before = list(inv.store.movements_log)

    await reserve(inv, {STOCKED: 2})

    assert inv.store.movements_log == before


async def test_reserve_publishes_stock_changed_with_the_available_units() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    inv.events.published.clear()
    inv.clock.current += timedelta(minutes=1)

    await reserve(inv, {STOCKED: 2})

    [event] = events(inv)
    assert (event.presentation_id, event.available, event.threshold) == (STOCKED, 3, 3)
    assert event.as_of == inv.clock.now()


async def test_reserve_publishes_one_event_per_changed_item() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 4)
    inv.events.published.clear()

    await reserve(inv, {STOCKED_2: 1, STOCKED: 2})

    assert sorted((e.presentation_id, e.available) for e in events(inv)) == [
        (STOCKED, 3),
        (STOCKED_2, 3),
    ]


async def test_reserve_accepts_exactly_the_available_units() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await reserve(inv, {STOCKED: 2}, "QA-1")

    await reserve(inv, {STOCKED: 3}, "QA-2")

    assert inv.store.items[STOCKED].available == 0


async def test_reserve_refuses_more_than_available_listing_the_short_line() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await reserve(inv, {STOCKED: 2}, "QA-1")
    inv.events.published.clear()

    error = refusal(await inv.reserve.execute("QA-2", {STOCKED: 4}, inv.clock.now() + timedelta(1)))

    assert error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert error.details == {
        "lines": [{"presentation_id": str(STOCKED), "requested": 4, "available": 3}]
    }
    assert unit_state(inv, STOCKED) == (5, 2)
    assert len(inv.store.reservations.by_id) == 1
    assert inv.events.published == []


async def test_reserve_is_all_or_nothing_and_lists_only_the_short_lines() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 3)

    error = refusal(
        await inv.reserve.execute(
            "QA-1", {STOCKED: 1, STOCKED_2: 10}, inv.clock.now() + timedelta(minutes=2)
        )
    )

    assert error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert error.details == {
        "lines": [{"presentation_id": str(STOCKED_2), "requested": 10, "available": 3}]
    }
    assert unit_state(inv, STOCKED) == (5, 0)  # the line that fit was not held
    assert unit_state(inv, STOCKED_2) == (3, 0)
    assert inv.store.reservations.by_id == {}


async def test_reserve_lists_every_short_line_not_just_the_first() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 1)
    await restock(inv, STOCKED_2, 2)

    error = refusal(
        await inv.reserve.execute(
            "QA-1", {STOCKED: 5, STOCKED_2: 6}, inv.clock.now() + timedelta(minutes=2)
        )
    )

    assert error.details == {
        "lines": [
            {"presentation_id": str(STOCKED), "requested": 5, "available": 1},
            {"presentation_id": str(STOCKED_2), "requested": 6, "available": 2},
        ]
    }


async def test_reserve_of_a_never_counted_presentation_is_unavailable_with_zero() -> None:
    inv = Inventory()

    error = refusal(await inv.reserve.execute("QA-1", {STOCKED: 1}, inv.clock.now() + timedelta(1)))

    assert error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert error.details == {
        "lines": [{"presentation_id": str(STOCKED), "requested": 1, "available": 0}]
    }
    assert STOCKED not in inv.store.items


async def test_reserve_refuses_a_reference_already_taken_and_holds_nothing_more() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await reserve(inv, {STOCKED: 2}, "QA-1")
    inv.events.published.clear()

    error = refusal(await inv.reserve.execute("QA-1", {STOCKED: 1}, inv.clock.now() + timedelta(1)))

    assert error.code == "INVENTORY_RESERVATION_REFERENCE_TAKEN"
    assert unit_state(inv, STOCKED) == (5, 2)
    assert len(inv.store.reservations.by_id) == 1
    assert inv.events.published == []


async def test_reserve_treats_a_reference_with_stray_spaces_as_the_same_reference() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await reserve(inv, {STOCKED: 1}, "QA-1")

    error = refusal(
        await inv.reserve.execute("  QA-1 ", {STOCKED: 1}, inv.clock.now() + timedelta(1))
    )

    assert error.code == "INVENTORY_RESERVATION_REFERENCE_TAKEN"


@pytest.mark.parametrize(
    ("presentation_id", "code"),
    [
        (MADE_TO_ORDER, "INVENTORY_PRESENTATION_NOT_STOCKED"),
        (UNKNOWN, "INVENTORY_PRESENTATION_NOT_FOUND"),
    ],
)
async def test_reserve_refuses_a_presentation_that_is_not_stocked_or_unknown(
    presentation_id: UUID, code: str
) -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)

    error = refusal(
        await inv.reserve.execute(
            "QA-1", {STOCKED: 1, presentation_id: 1}, inv.clock.now() + timedelta(minutes=2)
        )
    )

    assert error.code == code
    assert unit_state(inv, STOCKED) == (5, 0)
    assert inv.store.reservations.by_id == {}


@pytest.mark.parametrize(
    ("reference", "quantities", "minutes", "code"),
    [
        ("", {STOCKED: 1}, 2, "INVENTORY_RESERVATION_INVALID"),
        ("QA-1", {}, 2, "INVENTORY_RESERVATION_INVALID"),
        ("QA-1", {STOCKED: 0}, 2, "INVENTORY_RESERVATION_INVALID"),
        ("QA-1", {STOCKED: 1}, 0, "INVENTORY_RESERVATION_EXPIRY_INVALID"),
        ("QA-1", {STOCKED: 1}, -5, "INVENTORY_RESERVATION_EXPIRY_INVALID"),
    ],
)
async def test_reserve_refuses_invalid_input_before_touching_anything(
    reference: str, quantities: dict[UUID, int], minutes: int, code: str
) -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    inv.events.published.clear()

    error = refusal(
        await inv.reserve.execute(
            reference, quantities, inv.clock.now() + timedelta(minutes=minutes)
        )
    )

    assert error.code == code
    assert unit_state(inv, STOCKED) == (5, 0)
    assert inv.store.reservations.by_id == {}
    assert inv.events.published == []


async def test_reserve_uses_the_global_threshold_of_the_settings_in_its_event() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await inv.update_settings.execute(12)
    inv.events.published.clear()

    await reserve(inv, {STOCKED: 1})

    [event] = events(inv)
    assert (event.available, event.threshold) == (4, 12)


# --- ExtendReservation -------------------------------------------------------------------------


async def test_extend_moves_the_expiry_and_publishes_no_event() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.events.published.clear()
    new_expiry = inv.clock.now() + timedelta(hours=72)

    result = await inv.extend.execute(reservation_id, new_expiry)

    assert isinstance(result, Ok)
    assert inv.store.reservations.by_id[reservation_id].expires_at == new_expiry
    assert unit_state(inv, STOCKED) == (5, 2)
    assert inv.events.published == []


async def test_extend_to_an_earlier_or_equal_time_is_refused() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    current = inv.store.reservations.by_id[reservation_id].expires_at

    for candidate in (current, current - timedelta(seconds=1)):
        error = refusal(await inv.extend.execute(reservation_id, candidate))
        assert error.code == "INVENTORY_RESERVATION_EXPIRY_INVALID"
    assert inv.store.reservations.by_id[reservation_id].expires_at == current


async def test_extend_a_reservation_past_its_expiry_is_not_active() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.clock.current += timedelta(minutes=3)  # past the expiry, the job has not run yet

    error = refusal(await inv.extend.execute(reservation_id, inv.clock.now() + timedelta(days=1)))

    assert error.code == "INVENTORY_RESERVATION_NOT_ACTIVE"


@pytest.mark.parametrize("closing", ["release", "commit"])
async def test_extend_a_closed_reservation_is_not_active(closing: str) -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await getattr(inv, closing).execute(reservation_id)

    error = refusal(await inv.extend.execute(reservation_id, inv.clock.now() + timedelta(days=1)))

    assert error.code == "INVENTORY_RESERVATION_NOT_ACTIVE"


async def test_extend_an_unknown_reservation_is_not_found() -> None:
    inv = Inventory()

    error = refusal(await inv.extend.execute(UNKNOWN, inv.clock.now() + timedelta(days=1)))

    assert error.code == "INVENTORY_RESERVATION_NOT_FOUND"


# --- CommitReservation -------------------------------------------------------------------------


async def test_commit_sells_the_held_units_with_one_sale_movement() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2}, "QA-1")

    result = await inv.commit.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (3, 0)
    sale = inv.store.movements_log[-1]
    assert (sale.kind, sale.delta, sale.quantity, sale.on_hand_after) == (
        MovementKind.SALE,
        -2,
        2,
        3,
    )
    assert (sale.reason.value, sale.actor_id) == ("Venta: QA-1", "system")
    assert sale.created_at == inv.clock.now()
    stored = inv.store.reservations.by_id[reservation_id]
    assert (stored.status, stored.closed_at) == (ReservationStatus.COMMITTED, inv.clock.now())


async def test_commit_writes_one_sale_per_presentation() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 4)
    reservation_id = await reserve(inv, {STOCKED: 2, STOCKED_2: 3})
    before = len(inv.store.movements_log)

    await inv.commit.execute(reservation_id)

    sales = inv.store.movements_log[before:]
    assert sorted((m.presentation_id, m.delta) for m in sales) == [(STOCKED, -2), (STOCKED_2, -3)]
    assert unit_state(inv, STOCKED_2) == (1, 0)


async def test_commit_publishes_available_per_changed_item() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.events.published.clear()

    await inv.commit.execute(reservation_id)

    [event] = events(inv)
    assert (event.presentation_id, event.available) == (STOCKED, 3)


async def test_commit_twice_is_a_no_op_ok() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await inv.commit.execute(reservation_id)
    movements, published = len(inv.store.movements_log), len(inv.events.published)

    second = await inv.commit.execute(reservation_id)

    assert isinstance(second, Ok)
    assert unit_state(inv, STOCKED) == (3, 0)
    assert (len(inv.store.movements_log), len(inv.events.published)) == (movements, published)


async def test_commit_of_an_active_reservation_past_its_expiry_still_sells_the_held_units() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 2)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.clock.current += timedelta(minutes=10)  # expired in time, but the job has not run

    result = await inv.commit.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (0, 0)


async def test_commit_an_unknown_reservation_is_not_found() -> None:
    inv = Inventory()

    assert refusal(await inv.commit.execute(UNKNOWN)).code == "INVENTORY_RESERVATION_NOT_FOUND"


async def test_a_late_commit_of_an_expired_reservation_sells_when_units_are_available() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.clock.current += timedelta(minutes=3)
    assert await inv.expire.run_once() == 1
    assert unit_state(inv, STOCKED) == (5, 0)

    result = await inv.commit.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (3, 0)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.COMMITTED
    assert inv.store.movements_log[-1].kind is MovementKind.SALE


async def test_a_late_commit_with_no_units_available_is_refused_and_stays_expired() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 2)
    reservation_id = await reserve(inv, {STOCKED: 2}, "QA-1")
    inv.clock.current += timedelta(minutes=3)
    await inv.expire.run_once()
    await reserve(inv, {STOCKED: 2}, "QA-2")  # another order took the units
    movements, published = len(inv.store.movements_log), len(inv.events.published)

    error = refusal(await inv.commit.execute(reservation_id))

    assert error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert error.details == {
        "lines": [{"presentation_id": str(STOCKED), "requested": 2, "available": 0}]
    }
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.EXPIRED
    assert unit_state(inv, STOCKED) == (2, 2)
    assert (len(inv.store.movements_log), len(inv.events.published)) == (movements, published)


async def test_a_late_commit_of_a_released_reservation_sells_when_units_are_available() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await inv.release.execute(reservation_id)

    result = await inv.commit.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (3, 0)


async def test_a_late_commit_leaves_the_other_reservations_holds_alone() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    expired = await reserve(inv, {STOCKED: 2}, "QA-1")
    inv.clock.current += timedelta(minutes=3)
    await inv.expire.run_once()
    await reserve(inv, {STOCKED: 2}, "QA-2", minutes=60)

    result = await inv.commit.execute(expired)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (3, 2)


async def test_a_loss_below_reserved_is_recorded_and_available_floors_at_zero() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 3)
    await reserve(inv, {STOCKED: 2})
    inv.events.published.clear()

    await lose(inv, STOCKED, 2)

    assert unit_state(inv, STOCKED) == (1, 2)
    assert inv.store.items[STOCKED].available == 0
    [event] = events(inv)
    assert event.available == 0


async def test_committing_a_reservation_a_loss_left_short_is_refused_and_changes_nothing() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 3)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await lose(inv, STOCKED, 2)
    movements, published = len(inv.store.movements_log), len(inv.events.published)

    error = refusal(await inv.commit.execute(reservation_id))

    assert error.code == "INVENTORY_STOCK_INSUFFICIENT"
    assert error.details == {
        "lines": [{"presentation_id": str(STOCKED), "requested": 2, "on_hand": 1}]
    }
    assert unit_state(inv, STOCKED) == (1, 2)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.ACTIVE
    assert (len(inv.store.movements_log), len(inv.events.published)) == (movements, published)


async def test_a_refused_commit_sells_no_line_even_when_another_line_could_be_sold() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 3)
    reservation_id = await reserve(inv, {STOCKED: 2, STOCKED_2: 2})
    await lose(inv, STOCKED_2, 2)  # B: on_hand 1 < 2 held
    movements = len(inv.store.movements_log)

    error = refusal(await inv.commit.execute(reservation_id))

    assert error.code == "INVENTORY_STOCK_INSUFFICIENT"
    assert error.details == {
        "lines": [{"presentation_id": str(STOCKED_2), "requested": 2, "on_hand": 1}]
    }
    assert unit_state(inv, STOCKED) == (5, 2)  # A untouched
    assert len(inv.store.movements_log) == movements


# --- ReleaseReservation ------------------------------------------------------------------------


async def test_release_returns_the_units_and_records_no_movement() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    movements = len(inv.store.movements_log)
    inv.events.published.clear()

    result = await inv.release.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (5, 0)
    assert inv.store.items[STOCKED].available == 5
    assert len(inv.store.movements_log) == movements
    stored = inv.store.reservations.by_id[reservation_id]
    assert (stored.status, stored.closed_at) == (ReservationStatus.RELEASED, inv.clock.now())
    [event] = events(inv)
    assert (event.presentation_id, event.available) == (STOCKED, 5)


async def test_release_twice_is_a_no_op_ok() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await inv.release.execute(reservation_id)
    published = len(inv.events.published)

    second = await inv.release.execute(reservation_id)

    assert isinstance(second, Ok)
    assert unit_state(inv, STOCKED) == (5, 0)
    assert len(inv.events.published) == published


async def test_release_of_an_expired_reservation_is_a_no_op_ok() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    inv.clock.current += timedelta(minutes=3)
    await inv.expire.run_once()

    result = await inv.release.execute(reservation_id)

    assert isinstance(result, Ok)
    assert unit_state(inv, STOCKED) == (5, 0)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.EXPIRED


async def test_release_of_a_committed_reservation_is_refused() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await inv.commit.execute(reservation_id)

    error = refusal(await inv.release.execute(reservation_id))

    assert error.code == "INVENTORY_RESERVATION_COMMITTED"
    assert unit_state(inv, STOCKED) == (3, 0)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.COMMITTED


async def test_release_an_unknown_reservation_is_not_found() -> None:
    inv = Inventory()

    assert refusal(await inv.release.execute(UNKNOWN)).code == "INVENTORY_RESERVATION_NOT_FOUND"


async def test_release_gives_back_every_line_and_publishes_one_event_per_item() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 4)
    reservation_id = await reserve(inv, {STOCKED: 2, STOCKED_2: 3})
    inv.events.published.clear()

    await inv.release.execute(reservation_id)

    assert unit_state(inv, STOCKED) == (5, 0)
    assert unit_state(inv, STOCKED_2) == (4, 0)
    assert sorted((e.presentation_id, e.available) for e in events(inv)) == [
        (STOCKED, 5),
        (STOCKED_2, 4),
    ]


# --- ExpireReservations (the job) --------------------------------------------------------------


async def test_the_job_expires_what_is_due_and_frees_the_units() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2}, minutes=2)
    inv.clock.current += timedelta(minutes=2)  # exactly at the expiry: due
    inv.events.published.clear()

    count = await inv.expire.run_once()

    assert count == 1
    assert unit_state(inv, STOCKED) == (5, 0)
    stored = inv.store.reservations.by_id[reservation_id]
    assert (stored.status, stored.closed_at) == (ReservationStatus.EXPIRED, inv.clock.now())
    [event] = events(inv)
    assert (event.presentation_id, event.available, event.as_of) == (STOCKED, 5, inv.clock.now())


async def test_the_job_leaves_a_reservation_that_is_not_due_yet() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2}, minutes=2)
    inv.clock.current += timedelta(minutes=1, seconds=59)
    inv.events.published.clear()

    assert await inv.expire.run_once() == 0

    assert unit_state(inv, STOCKED) == (5, 2)
    assert inv.store.reservations.by_id[reservation_id].status is ReservationStatus.ACTIVE
    assert inv.events.published == []


async def test_the_job_ignores_reservations_that_are_not_active() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    committed = await reserve(inv, {STOCKED: 1}, "QA-1")
    released = await reserve(inv, {STOCKED: 2}, "QA-2")
    await inv.commit.execute(committed)
    await inv.release.execute(released)
    inv.clock.current += timedelta(hours=1)
    inv.events.published.clear()

    assert await inv.expire.run_once() == 0

    assert unit_state(inv, STOCKED) == (9, 0)
    assert inv.store.reservations.by_id[committed].status is ReservationStatus.COMMITTED
    assert inv.store.reservations.by_id[released].status is ReservationStatus.RELEASED
    assert inv.events.published == []


async def test_the_job_expires_several_and_publishes_one_event_per_changed_item() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    await restock(inv, STOCKED_2, 10)
    await reserve(inv, {STOCKED: 2, STOCKED_2: 1}, "QA-1")
    await reserve(inv, {STOCKED: 3}, "QA-2")
    inv.clock.current += timedelta(minutes=5)
    inv.events.published.clear()

    count = await inv.expire.run_once()

    assert count == 2
    assert unit_state(inv, STOCKED) == (10, 0)
    assert unit_state(inv, STOCKED_2) == (10, 0)
    assert sorted((e.presentation_id, e.available) for e in events(inv)) == [
        (STOCKED, 10),
        (STOCKED_2, 10),
    ]


async def test_the_job_works_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reservation_commands, "EXPIRE_BATCH", 2)
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    for number in range(3):
        await reserve(inv, {STOCKED: 1}, f"QA-{number}")
    inv.clock.current += timedelta(minutes=5)

    assert await inv.expire.run_once() == 2
    assert unit_state(inv, STOCKED) == (10, 1)
    assert await inv.expire.run_once() == 1
    assert unit_state(inv, STOCKED) == (10, 0)
    assert await inv.expire.run_once() == 0


async def test_an_expiry_frees_units_for_a_new_reservation() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 2)
    await reserve(inv, {STOCKED: 2}, "QA-1")
    refused = await inv.reserve.execute("QA-2", {STOCKED: 1}, inv.clock.now() + timedelta(1))
    assert isinstance(refused, Err)
    inv.clock.current += timedelta(minutes=3)
    await inv.expire.run_once()

    result = await inv.reserve.execute("QA-2", {STOCKED: 1}, inv.clock.now() + timedelta(1))

    assert isinstance(result, Ok)


# --- the facade --------------------------------------------------------------------------------


async def test_the_facade_is_exported_from_the_module_public_api() -> None:
    assert StockReservations is type(Inventory().facade)


async def test_the_facade_drives_the_whole_lifecycle_and_returns_each_result() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)

    reserved = await inv.facade.reserve("QA-1", {STOCKED: 2}, inv.clock.now() + timedelta(1))
    assert isinstance(reserved, Ok)
    extended = await inv.facade.extend(reserved.value, inv.clock.now() + timedelta(days=3))
    assert isinstance(extended, Ok)
    committed = await inv.facade.commit(reserved.value)
    assert isinstance(committed, Ok)
    released = await inv.facade.release(reserved.value)

    assert unit_state(inv, STOCKED) == (3, 0)
    assert refusal(released).code == "INVENTORY_RESERVATION_COMMITTED"


async def test_the_facade_passes_a_refusal_through() -> None:
    inv = Inventory()

    result = await inv.facade.reserve("QA-1", {STOCKED: 1}, inv.clock.now() + timedelta(1))

    assert refusal(result).code == "INVENTORY_STOCK_UNAVAILABLE"
