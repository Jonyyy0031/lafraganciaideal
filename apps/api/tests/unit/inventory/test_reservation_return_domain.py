"""Plan 004 step 1, domain: `Reservation.mark_returned`, the `returned` status and its ledger
reason."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.inventory.domain.errors import ReservationNotCommitted
from fragancia_api.modules.inventory.domain.reservation import Reservation, ReservationStatus
from fragancia_api.shared.kernel import Ok

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=2)
A = UUID(int=1)


def reservation(reference: str = "order:abc") -> Reservation:
    result = Reservation.create(reference, {A: 2}, LATER, NOW)
    assert isinstance(result, Ok)
    return result.value


def test_the_returned_status_is_the_string_returned() -> None:
    assert ReservationStatus.RETURNED.value == "returned"


def test_a_committed_reservation_can_be_marked_returned() -> None:
    created = reservation()
    created.commit(NOW)

    created.mark_returned(LATER)

    assert created.status is ReservationStatus.RETURNED
    assert created.closed_at == LATER


@pytest.mark.parametrize("first", ["release", "expire"])
def test_a_released_or_expired_reservation_cannot_be_marked_returned(first: str) -> None:
    created = reservation()
    getattr(created, first)(NOW)
    status = created.status

    with pytest.raises(ValueError):
        created.mark_returned(LATER)
    assert (created.status, created.closed_at) == (status, NOW)


def test_an_active_reservation_cannot_be_marked_returned() -> None:
    created = reservation()

    with pytest.raises(ValueError):
        created.mark_returned(LATER)
    assert created.status is ReservationStatus.ACTIVE


def test_a_returned_reservation_cannot_be_marked_returned_again() -> None:
    created = reservation()
    created.commit(NOW)
    created.mark_returned(NOW)

    with pytest.raises(ValueError):
        created.mark_returned(LATER)
    assert created.closed_at == NOW


def test_a_returned_reservation_no_longer_holds_units() -> None:
    created = reservation()
    created.commit(NOW)
    created.mark_returned(LATER)

    assert created.holds_units(LATER) is False


def test_the_return_reason_is_the_spanish_copy_with_the_reference() -> None:
    assert reservation("order:xyz").return_reason() == "Devolución: order:xyz"


def test_the_not_committed_error_has_its_code() -> None:
    assert ReservationNotCommitted().code == "INVENTORY_RESERVATION_NOT_COMMITTED"
