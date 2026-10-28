"""Payments plan 003 step 10 (decision 22), orders: `pay_by` flows from `Order.await_payment` and
the `hold_for_payment` facade into `OrderPaymentPending`, and the instructions email prints it
(not the order's deadline) whenever the two differ."""

from datetime import UTC, datetime, timedelta

from fragancia_api.modules.orders.application.commands.confirm_payment import (
    OrderPaymentConfirmations,
)
from fragancia_api.modules.orders.application.handlers.order_emails import (
    SendPaymentInstructionsEmail,
    payment_instructions_email,
)
from fragancia_api.modules.orders.domain.order import OrderPaymentPending
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import RecordingEmailSender
from tests.unit.orders.support import NOW
from tests.unit.orders.test_confirm_payment import ORDER_ID
from tests.unit.orders.test_order_emails import LINK, LINKS, STOREFRONT, FakeQueries, make_detail
from tests.unit.orders.test_order_lifecycle import World
from tests.unit.orders.test_order_lifecycle_domain import make_order

# 2026-10-29 18:00 UTC is 13:00 in Cancún; 2026-10-31 18:00 UTC is 13:00 on the 31st.
PAY_BY = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
DEADLINE = PAY_BY + timedelta(hours=48)


def test_await_payment_records_the_pay_by_apart_from_the_deadline() -> None:
    order = make_order()

    order.await_payment(DEADLINE, PAY_BY, "efectivo en tienda", None, NOW)

    [event] = order.pull_events()
    assert isinstance(event, OrderPaymentPending)
    assert (event.due_at, event.pay_by) == (DEADLINE, PAY_BY)
    assert order.payment_due_at == DEADLINE
    assert event.payload()["pay_by"] == PAY_BY.isoformat()
    assert event.payload()["due_at"] == DEADLINE.isoformat()


def test_the_pay_by_is_recorded_as_given_even_when_the_deadline_does_not_move() -> None:
    order = make_order()
    before = order.payment_due_at
    earlier = before - timedelta(hours=1)

    order.await_payment(earlier, earlier, "efectivo en tienda", None, NOW)

    [event] = order.pull_events()
    assert isinstance(event, OrderPaymentPending)
    assert (event.due_at, event.pay_by) == (before, earlier)


async def test_the_facade_publishes_the_pay_by_and_the_deadline_separately() -> None:
    world = World()
    await world.store()
    facade = world.wire(OrderPaymentConfirmations)

    await facade.hold_for_payment(ORDER_ID, DEADLINE, PAY_BY, "efectivo en tienda", None)

    [event] = [e for e in world.events.published if isinstance(e, OrderPaymentPending)]
    assert (event.due_at, event.pay_by) == (DEADLINE, PAY_BY)
    assert world.current().payment_due_at == DEADLINE
    assert world.stocks.extensions == [(world.current().reservation_id, DEADLINE)]


# --- the email ---------------------------------------------------------------------------------


def test_the_email_prints_the_pay_by_not_the_order_deadline() -> None:
    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", PAY_BY, None)

    assert "antes del 29/10/2026 a las 13:00." in body
    assert "31/10/2026" not in body


async def test_the_handler_prints_the_pay_by_of_the_event_when_it_differs_from_the_deadline() -> (
    None
):
    e = OrderPaymentPending(
        order_id=ORDER_ID,
        number=1001,
        due_at=DEADLINE,
        pay_by=PAY_BY,
        method="efectivo en tienda",
        instructions_url=None,
    )
    email = RecordingEmailSender()
    handler = SendPaymentInstructionsEmail(
        queries=FakeQueries(make_detail()), links=LINKS, email=email, storefront_url=STOREFRONT
    )

    await handler(
        EventMessage(id=e.event_id, name=e.name, payload=e.payload(), occurred_at=e.occurred_at)
    )

    [mail] = email.sent
    assert "antes del 29/10/2026 a las 13:00." in mail.body
    assert "31/10/2026" not in mail.body
