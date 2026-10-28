"""Plan 003 step 4, domain: the ticket fields of `Payment` (`needs_hold`, `mark_held`,
`is_live_ticket`, `describe`) and the words of the cash types (payments/domain/payment.py)."""

from datetime import timedelta

import pytest

from fragancia_api.modules.payments.domain.events import CheckoutCloseRequested
from fragancia_api.modules.payments.domain.payment import (
    CASH_TYPES,
    METHOD_LABELS,
    PENDING,
    Payment,
)
from tests.unit.payments.support import NOW, ORDER_ID

EXPIRES = NOW + timedelta(hours=48)


def ticket(**overrides: object) -> Payment:
    values: dict[str, object] = {
        "payment_id": "777",
        "order_id": ORDER_ID,
        "status": "pending",
        "status_detail": "pending_waiting_payment",
        "amount_cents": 268_000,
        "currency": "MXN",
        "payment_type": "ticket",
        "approved_at": None,
        "outcome": None,
        "created_at": NOW,
        "updated_at": NOW,
        "payment_method": "oxxo",
        "expires_at": EXPIRES,
        "ticket_url": "https://mp.test/ticket/777",
    }
    values.update(overrides)
    return Payment(**values)  # type: ignore[arg-type]


def test_the_stored_words_of_the_ticket_types_and_the_pending_status() -> None:
    assert PENDING == "pending"
    assert CASH_TYPES == ("ticket", "bank_transfer")
    assert METHOD_LABELS == {
        "ticket": "efectivo en tienda",
        "bank_transfer": "transferencia SPEI",
    }


def test_the_ticket_fields_default_to_empty() -> None:
    payment = Payment(
        payment_id="1",
        order_id=None,
        status="approved",
        status_detail=None,
        amount_cents=1,
        currency="MXN",
        payment_type="credit_card",
        approved_at=None,
        outcome=None,
        created_at=NOW,
        updated_at=NOW,
    )

    assert (payment.payment_method, payment.expires_at) == (None, None)
    assert (payment.ticket_url, payment.held_at) == (None, None)


@pytest.mark.parametrize("kind", ["ticket", "bank_transfer"])
def test_a_pending_cash_ticket_with_an_expiry_not_held_yet_needs_a_hold(kind: str) -> None:
    assert ticket(payment_type=kind).needs_hold(NOW) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "cancelled"},
        {"payment_type": "credit_card"},
        {"payment_type": "atm"},
        {"payment_type": None},
        {"expires_at": None},
        {"held_at": NOW},
    ],
)
def test_anything_else_needs_no_hold(overrides: dict[str, object]) -> None:
    assert ticket(**overrides).needs_hold(NOW) is False


def test_mark_held_sets_held_at_and_updated_at_and_stops_needing_a_hold() -> None:
    payment = ticket()
    later = NOW + timedelta(minutes=2)

    payment.mark_held(later)

    assert (payment.held_at, payment.updated_at) == (later, later)
    assert payment.created_at == NOW
    assert payment.needs_hold(NOW) is False


def test_describe_takes_the_method_the_expiry_and_the_page_only() -> None:
    payment = ticket(payment_method=None, expires_at=None, ticket_url=None)

    payment.describe("spei", EXPIRES, "https://mp.test/x")

    assert (payment.payment_method, payment.expires_at, payment.ticket_url) == (
        "spei",
        EXPIRES,
        "https://mp.test/x",
    )
    assert (payment.status, payment.held_at, payment.updated_at) == ("pending", None, NOW)


def test_a_live_ticket_is_pending_cash_and_expires_after_now() -> None:
    assert ticket().is_live_ticket(NOW) is True
    assert ticket(payment_type="bank_transfer").is_live_ticket(NOW) is True


def test_a_ticket_expiring_at_or_before_now_is_not_live() -> None:
    assert ticket().is_live_ticket(EXPIRES) is False
    assert ticket().is_live_ticket(EXPIRES + timedelta(seconds=1)) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "expired"},
        {"payment_type": "credit_card"},
        {"expires_at": None},
    ],
)
def test_other_payments_are_never_live_tickets(overrides: dict[str, object]) -> None:
    assert ticket(**overrides).is_live_ticket(NOW) is False


def test_is_live_ticket_does_not_depend_on_having_been_held() -> None:
    assert ticket(held_at=NOW).is_live_ticket(NOW) is True


def test_the_close_requested_event_has_its_name_and_payload() -> None:
    event = CheckoutCloseRequested(order_id=ORDER_ID)

    assert CheckoutCloseRequested.name == "payments.checkout.close_requested"
    assert event.payload() == {"order_id": str(ORDER_ID)}
