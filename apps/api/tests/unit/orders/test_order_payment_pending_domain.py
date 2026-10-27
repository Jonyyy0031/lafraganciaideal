"""Payments plan 003 step 3, domain: `Order.await_payment` (a later deadline moves it, an earlier
or equal one does not, only a pending order, the event is recorded every time and the status
and history never change) and the `orders.order.payment_pending` event."""

from datetime import timedelta

import pytest

from fragancia_api.modules.orders.domain.order import OrderPaymentPending, OrderStatus
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.orders.support import NOW
from tests.unit.orders.test_order_lifecycle_domain import make_order

LATER = NOW + timedelta(hours=1)
URL = "https://mp.test/ticket/1"


def test_a_later_deadline_moves_the_payment_due_at_and_answers_true() -> None:
    order = make_order()
    until = order.payment_due_at + timedelta(hours=47)

    result = order.await_payment(until, "efectivo en tienda", URL, LATER)

    assert result == Ok(True)
    assert order.payment_due_at == until
    assert order.updated_at == LATER


@pytest.mark.parametrize("delta", [timedelta(0), -timedelta(seconds=1), -timedelta(hours=5)])
def test_an_equal_or_earlier_deadline_does_not_move_it_and_answers_false(
    delta: timedelta,
) -> None:
    order = make_order()
    before = order.payment_due_at
    updated = order.updated_at

    result = order.await_payment(before + delta, "efectivo en tienda", None, LATER)

    assert result == Ok(False)
    assert order.payment_due_at == before
    assert order.updated_at == updated


def test_one_second_later_is_enough_to_move() -> None:
    order = make_order()

    result = order.await_payment(
        order.payment_due_at + timedelta(seconds=1), "transferencia SPEI", None, LATER
    )

    assert result == Ok(True)


def test_it_always_records_the_event_with_the_new_deadline_the_method_and_the_page() -> None:
    order = make_order()
    until = order.payment_due_at + timedelta(hours=47)

    order.await_payment(until, "efectivo en tienda", URL, LATER)

    [event] = order.pull_events()
    assert isinstance(event, OrderPaymentPending)
    assert event.name == "orders.order.payment_pending"
    assert (event.order_id, event.number) == (order.id, order.number)
    assert (event.due_at, event.method, event.instructions_url) == (
        until,
        "efectivo en tienda",
        URL,
    )


def test_the_event_carries_the_unchanged_deadline_when_it_did_not_move() -> None:
    order = make_order()
    before = order.payment_due_at

    order.await_payment(before - timedelta(hours=1), "transferencia SPEI", None, LATER)

    [event] = order.pull_events()
    assert isinstance(event, OrderPaymentPending)
    assert event.due_at == before
    assert event.instructions_url is None
    assert event.method == "transferencia SPEI"


def test_the_event_payload_is_what_the_relay_hands_over() -> None:
    order = make_order()
    until = order.payment_due_at + timedelta(hours=2)
    order.await_payment(until, "efectivo en tienda", URL, LATER)

    [event] = order.pull_events()

    assert event.payload() == {
        "order_id": str(order.id),
        "number": order.number,
        "due_at": until.isoformat(),
        "method": "efectivo en tienda",
        "instructions_url": URL,
    }


def test_it_changes_neither_the_status_nor_the_history() -> None:
    order = make_order()

    order.await_payment(order.payment_due_at + timedelta(hours=2), "x", None, LATER)

    assert order.status is OrderStatus.PENDING_PAYMENT
    assert [e for e in order.pull_events() if e.name == "orders.order.status_changed"] == []


@pytest.mark.parametrize("status", [s for s in OrderStatus if s is not OrderStatus.PENDING_PAYMENT])
def test_an_order_that_is_not_pending_payment_is_refused_and_records_nothing(
    status: OrderStatus,
) -> None:
    order = make_order(status=status)
    before = order.payment_due_at

    result = order.await_payment(before + timedelta(hours=2), "x", None, LATER)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_TRANSITION_INVALID"
    assert result.error.details == {"from": status.value, "to": "pending_payment"}
    assert order.payment_due_at == before
    assert order.pull_events() == []


def test_two_tickets_move_the_deadline_to_the_latest_and_never_back() -> None:
    order = make_order()
    first = order.payment_due_at + timedelta(hours=40)
    second = order.payment_due_at + timedelta(hours=47)

    order.await_payment(first, "efectivo en tienda", None, LATER)
    order.await_payment(second, "transferencia SPEI", None, LATER)
    order.await_payment(first, "efectivo en tienda", None, LATER)

    assert order.payment_due_at == second
    assert len(order.pull_events()) == 3
