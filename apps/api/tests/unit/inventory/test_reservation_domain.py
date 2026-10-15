"""Plan 003, domain: `reserved`/`available` on the stock item, `hold`/`unhold`/`sell`, the `sale`
movement, and the `Reservation` aggregate (every transition, refused ones included)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.inventory.domain.errors import (
    ReservationExpiryInvalid,
    ReservationInvalid,
    ReservationNotActive,
    StockInsufficient,
    StockQuantityInvalid,
    StockUnavailable,
)
from fragancia_api.modules.inventory.domain.reservation import (
    RESERVATION_REFERENCE_MAX,
    SALE_ACTOR,
    Reservation,
    ReservationLine,
    ReservationStatus,
)
from fragancia_api.modules.inventory.domain.stock import (
    QUANTITY_MAX,
    Movement,
    MovementKind,
    Reason,
    StockItem,
    Threshold,
    available_units,
    is_low,
)
from fragancia_api.shared.kernel import Err, Ok

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=2)
PRESENTATION = UUID(int=7)
A, B, C = UUID(int=1), UUID(int=2), UUID(int=3)
REASON = Reason("Venta: PED-1")


def item(on_hand: int = 0, reserved: int = 0, override: int | None = None) -> StockItem:
    return StockItem(
        presentation_id=PRESENTATION,
        on_hand=on_hand,
        reserved=reserved,
        threshold_override=Threshold(override) if override is not None else None,
        updated_at=NOW,
    )


def reservation(
    quantities: dict[UUID, int] | None = None, *, reference: str = "PED-1"
) -> Reservation:
    result = Reservation.create(reference, quantities or {A: 2}, LATER, NOW)
    assert isinstance(result, Ok)
    return result.value


# --- available ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("on_hand", "reserved", "expected"),
    [(5, 2, 3), (5, 5, 0), (3, 5, 0), (1, 2, 0), (0, 0, 0)],
)
def test_available_is_on_hand_minus_reserved_never_below_zero(
    on_hand: int, reserved: int, expected: int
) -> None:
    assert available_units(on_hand, reserved) == expected
    assert item(on_hand, reserved).available == expected


def test_a_new_item_has_nothing_reserved() -> None:
    assert StockItem.empty(PRESENTATION, NOW).reserved == 0
    assert item(4).reserved == 0


def test_is_low_uses_available_and_counts_equality_as_low() -> None:
    assert is_low(3, None, 3) is True
    assert is_low(4, None, 3) is False
    assert is_low(2, 1, 3) is False  # the override 1 wins over the global 3
    assert is_low(1, 1, 3) is True


def test_a_reservation_makes_an_item_low_although_it_has_units_on_hand() -> None:
    stock = item(on_hand=10, reserved=7)

    assert stock.is_low(Threshold(3)) is True  # available 3 <= 3
    assert item(on_hand=10, reserved=6).is_low(Threshold(3)) is False  # available 4


def test_the_changed_event_carries_available_not_on_hand() -> None:
    event = item(on_hand=10, reserved=4).changed_event(Threshold(3), NOW)

    assert (event.available, event.threshold, event.as_of) == (6, 3, NOW)
    assert event.presentation_id == PRESENTATION


def test_the_changed_event_floors_available_at_zero() -> None:
    assert item(on_hand=1, reserved=2).changed_event(Threshold(3), NOW).available == 0


# --- hold / unhold -----------------------------------------------------------------------------


def test_hold_raises_reserved_and_lowers_available() -> None:
    stock = item(on_hand=5)

    result = stock.hold(2)

    assert isinstance(result, Ok)
    assert (stock.on_hand, stock.reserved, stock.available) == (5, 2, 3)


def test_hold_accepts_exactly_the_available_units() -> None:
    stock = item(on_hand=5, reserved=2)

    assert isinstance(stock.hold(3), Ok)
    assert (stock.reserved, stock.available) == (5, 0)


def test_hold_refuses_more_than_available_and_changes_nothing() -> None:
    stock = item(on_hand=5, reserved=2)

    result = stock.hold(4)

    assert isinstance(result, Err)
    assert isinstance(result.error, StockUnavailable)
    assert result.error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert stock.reserved == 2


def test_hold_refuses_when_a_loss_left_nothing_available() -> None:
    stock = item(on_hand=1, reserved=2)

    assert isinstance(stock.hold(1), Err)


def test_hold_writes_no_movement() -> None:
    stock = item(on_hand=5)
    stock.hold(2)

    assert stock.new_movements == []


def test_unhold_gives_units_back() -> None:
    stock = item(on_hand=5, reserved=3)

    stock.unhold(2)

    assert (stock.reserved, stock.available) == (1, 4)


def test_unhold_more_than_reserved_is_a_programming_error() -> None:
    stock = item(on_hand=5, reserved=1)

    with pytest.raises(ValueError):
        stock.unhold(2)
    assert stock.reserved == 1


# --- the sale movement -------------------------------------------------------------------------


@pytest.mark.parametrize("quantity", [1, 5])
def test_record_movement_refuses_a_sale_whatever_the_quantity(quantity: int) -> None:
    stock = item(on_hand=10)

    result = stock.record_movement(MovementKind.SALE, quantity, REASON, "actor", NOW)

    assert isinstance(result, Err)
    assert isinstance(result.error, StockQuantityInvalid)
    assert (stock.on_hand, stock.new_movements) == (10, [])


# --- sell --------------------------------------------------------------------------------------


def sold(stock: StockItem, quantity: int, *, held: bool) -> Movement:
    result = stock.sell(quantity, REASON, SALE_ACTOR, NOW, held=held)
    assert isinstance(result, Ok)
    return result.value


def test_a_held_sale_lowers_on_hand_and_consumes_the_hold() -> None:
    stock = item(on_hand=5, reserved=2)

    movement = sold(stock, 2, held=True)

    assert (stock.on_hand, stock.reserved, stock.available) == (3, 0, 3)
    assert (movement.kind, movement.quantity, movement.delta) == (MovementKind.SALE, 2, -2)
    assert (movement.on_hand_after, movement.actor_id, movement.reason) == (3, "system", REASON)
    assert movement.presentation_id == PRESENTATION
    assert stock.new_movements == [movement]
    assert stock.updated_at == NOW


def test_a_held_sale_needs_only_on_hand_not_available() -> None:
    stock = item(on_hand=2, reserved=2)  # available is 0, yet the units are this one's

    sold(stock, 2, held=True)

    assert (stock.on_hand, stock.reserved) == (0, 0)


def test_a_held_sale_refuses_when_a_loss_left_fewer_units_than_it_holds() -> None:
    stock = item(on_hand=1, reserved=2)

    result = stock.sell(2, REASON, SALE_ACTOR, NOW, held=True)

    assert isinstance(result, Err)
    assert isinstance(result.error, StockInsufficient)
    assert (stock.on_hand, stock.reserved, stock.new_movements) == (1, 2, [])


def test_a_late_sale_takes_available_units_and_leaves_other_holds_alone() -> None:
    stock = item(on_hand=5, reserved=2)  # another reservation holds 2

    movement = sold(stock, 3, held=False)

    assert (stock.on_hand, stock.reserved) == (2, 2)
    assert movement.delta == -3


def test_a_late_sale_refuses_beyond_available() -> None:
    stock = item(on_hand=5, reserved=3)

    result = stock.sell(3, REASON, SALE_ACTOR, NOW, held=False)

    assert isinstance(result, Err)
    assert isinstance(result.error, StockUnavailable)
    assert (stock.on_hand, stock.reserved, stock.new_movements) == (5, 3, [])


# --- Reservation.create ------------------------------------------------------------------------


def test_create_builds_an_active_reservation() -> None:
    result = Reservation.create("PED-1", {A: 2, B: 1}, LATER, NOW)

    assert isinstance(result, Ok)
    created = result.value
    assert created.status is ReservationStatus.ACTIVE
    assert (created.reference, created.expires_at, created.created_at) == ("PED-1", LATER, NOW)
    assert created.closed_at is None
    assert created.lines == (ReservationLine(A, 2), ReservationLine(B, 1))


def test_create_sorts_the_lines_by_presentation_id() -> None:
    created = reservation({C: 1, A: 1, B: 1})

    assert [line.presentation_id for line in created.lines] == [A, B, C]


def test_create_strips_the_reference() -> None:
    assert reservation(reference="  PED-1  ").reference == "PED-1"


@pytest.mark.parametrize("reference", ["x", "x" * RESERVATION_REFERENCE_MAX])
def test_create_accepts_the_reference_boundaries(reference: str) -> None:
    assert reservation(reference=reference).reference == reference


@pytest.mark.parametrize("reference", ["", "   ", "x" * (RESERVATION_REFERENCE_MAX + 1)])
def test_create_rejects_a_reference_outside_one_to_sixty_four(reference: str) -> None:
    result = Reservation.create(reference, {A: 1}, LATER, NOW)

    assert isinstance(result, Err)
    assert isinstance(result.error, ReservationInvalid)
    assert result.error.code == "INVENTORY_RESERVATION_INVALID"


def test_create_rejects_a_reservation_without_lines() -> None:
    result = Reservation.create("PED-1", {}, LATER, NOW)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_INVALID"


@pytest.mark.parametrize("quantity", [0, -1, QUANTITY_MAX + 1])
def test_create_rejects_a_quantity_outside_the_bounds(quantity: int) -> None:
    result = Reservation.create("PED-1", {A: 1, B: quantity}, LATER, NOW)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_INVALID"


@pytest.mark.parametrize("quantity", [1, QUANTITY_MAX])
def test_create_accepts_the_quantity_boundaries(quantity: int) -> None:
    assert reservation({A: quantity}).lines == (ReservationLine(A, quantity),)


@pytest.mark.parametrize("expires_at", [NOW, NOW - timedelta(seconds=1)])
def test_create_rejects_an_expiry_that_is_not_in_the_future(expires_at: datetime) -> None:
    result = Reservation.create("PED-1", {A: 1}, expires_at, NOW)

    assert isinstance(result, Err)
    assert isinstance(result.error, ReservationExpiryInvalid)
    assert result.error.code == "INVENTORY_RESERVATION_EXPIRY_INVALID"


def test_create_accepts_an_expiry_one_instant_ahead() -> None:
    assert isinstance(Reservation.create("PED-1", {A: 1}, NOW + timedelta(microseconds=1), NOW), Ok)


def test_the_sale_reason_names_the_reference_in_spanish() -> None:
    assert reservation(reference="QA-1").sale_reason() == "Venta: QA-1"


def test_the_sale_actor_is_the_system() -> None:
    assert SALE_ACTOR == "system"


# --- holds_units -------------------------------------------------------------------------------


def test_an_active_reservation_holds_its_units_even_past_its_expiry() -> None:
    created = reservation()

    assert created.holds_units(NOW) is True
    assert created.holds_units(LATER + timedelta(hours=1)) is True


@pytest.mark.parametrize("close", ["commit", "release", "expire"])
def test_a_closed_reservation_holds_nothing(close: str) -> None:
    created = reservation()
    getattr(created, close)(LATER)

    assert created.holds_units(LATER) is False


# --- extend ------------------------------------------------------------------------------------


def test_extend_moves_the_expiry_later() -> None:
    created = reservation()
    later = LATER + timedelta(hours=70)

    result = created.extend(later, NOW)

    assert isinstance(result, Ok)
    assert created.expires_at == later
    assert created.status is ReservationStatus.ACTIVE


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_extend_refuses_an_expiry_not_later_than_the_current_one(delta: timedelta) -> None:
    created = reservation()

    result = created.extend(LATER + delta, NOW)

    assert isinstance(result, Err)
    assert isinstance(result.error, ReservationExpiryInvalid)
    assert created.expires_at == LATER


def test_extend_refuses_a_reservation_at_or_past_its_expiry() -> None:
    for moment in (LATER, LATER + timedelta(seconds=1)):
        created = reservation()

        result = created.extend(LATER + timedelta(days=1), moment)

        assert isinstance(result, Err)
        assert isinstance(result.error, ReservationNotActive)
        assert result.error.code == "INVENTORY_RESERVATION_NOT_ACTIVE"
        assert created.expires_at == LATER


@pytest.mark.parametrize("close", ["commit", "release", "expire"])
def test_extend_refuses_a_closed_reservation(close: str) -> None:
    created = reservation()
    getattr(created, close)(NOW)

    result = created.extend(LATER + timedelta(days=1), NOW)

    assert isinstance(result, Err)
    assert isinstance(result.error, ReservationNotActive)


def test_extend_reports_not_active_before_a_bad_expiry() -> None:
    created = reservation()
    created.release(NOW)

    result = created.extend(NOW, NOW)  # both rules broken

    assert isinstance(result, Err)
    assert isinstance(result.error, ReservationNotActive)


# --- commit / release / expire -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("close", "status"),
    [
        ("commit", ReservationStatus.COMMITTED),
        ("release", ReservationStatus.RELEASED),
        ("expire", ReservationStatus.EXPIRED),
    ],
)
def test_an_active_reservation_can_be_closed_three_ways(
    close: str, status: ReservationStatus
) -> None:
    created = reservation()

    getattr(created, close)(LATER)

    assert created.status is status
    assert created.closed_at == LATER


@pytest.mark.parametrize("first", ["release", "expire"])
def test_a_released_or_expired_reservation_can_still_be_committed(first: str) -> None:
    created = reservation()
    getattr(created, first)(NOW)

    created.commit(LATER)

    assert created.status is ReservationStatus.COMMITTED
    assert created.closed_at == LATER


def test_committing_a_committed_reservation_is_a_programming_error() -> None:
    created = reservation()
    created.commit(NOW)

    with pytest.raises(ValueError):
        created.commit(LATER)
    assert created.closed_at == NOW


@pytest.mark.parametrize("first", ["commit", "release", "expire"])
def test_release_only_applies_to_an_active_reservation(first: str) -> None:
    created = reservation()
    getattr(created, first)(NOW)
    status = created.status

    with pytest.raises(ValueError):
        created.release(LATER)
    assert (created.status, created.closed_at) == (status, NOW)


@pytest.mark.parametrize("first", ["commit", "release", "expire"])
def test_expire_only_applies_to_an_active_reservation(first: str) -> None:
    created = reservation()
    getattr(created, first)(NOW)
    status = created.status

    with pytest.raises(ValueError):
        created.expire(LATER)
    assert (created.status, created.closed_at) == (status, NOW)
