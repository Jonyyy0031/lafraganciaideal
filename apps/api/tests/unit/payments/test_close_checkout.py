"""Plan 003 step 6 (and deviation 8), application: `RequestCheckoutClose` (which status moves
request a close) and `CloseCheckout` (which checkout is closed, no checkout, Mercado Pago not
knowing the preference, a gateway failure that raises and leaves the checkout open)."""

from datetime import timedelta
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.handlers.close_checkout import (
    CloseCheckout,
    RequestCheckoutClose,
)
from fragancia_api.modules.payments.domain.events import CheckoutCloseRequested
from fragancia_api.modules.payments.infrastructure.in_memory import (
    FakeGateway,
    InMemoryCheckouts,
    InMemoryPayments,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from tests.unit.payments.support import NOW, ORDER_ID, make_checkout


def status_changed(from_status: str, to_status: str, order_id: UUID = ORDER_ID) -> EventMessage:
    return EventMessage(
        id=UUID(int=5),
        name="orders.order.status_changed",
        payload={
            "order_id": str(order_id),
            "number": 1001,
            "from_status": from_status,
            "to_status": to_status,
        },
        occurred_at=NOW,
    )


def close_requested(order_id: UUID = ORDER_ID) -> EventMessage:
    event = CheckoutCloseRequested(order_id=order_id)
    return EventMessage(
        id=event.event_id, name=event.name, payload=event.payload(), occurred_at=event.occurred_at
    )


# --- RequestCheckoutClose ----------------------------------------------------------------------


def requester() -> tuple[RequestCheckoutClose, RecordingEventPublisher]:
    events = RecordingEventPublisher()
    return RequestCheckoutClose(events=events, transactions=InMemoryTransactionRunner()), events


@pytest.mark.parametrize("to_status", ["paid", "cancelled"])
async def test_leaving_pending_payment_for_paid_or_cancelled_requests_the_close(
    to_status: str,
) -> None:
    handler, events = requester()

    await handler(status_changed("pending_payment", to_status))

    assert [e.payload() for e in events.published] == [{"order_id": str(ORDER_ID)}]
    assert [e.name for e in events.published] == ["payments.checkout.close_requested"]


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("paid", "preparing"),
        ("paid", "shipped"),
        ("paid", "ready_for_pickup"),
        ("paid", "cancelled"),
        ("preparing", "shipped"),
        ("shipped", "delivered"),
        ("ready_for_pickup", "delivered"),
        ("cancelled", "cancelled"),
    ],
)
async def test_any_other_move_requests_nothing(from_status: str, to_status: str) -> None:
    handler, events = requester()

    await handler(status_changed(from_status, to_status))

    assert events.published == []


async def test_the_request_publishes_inside_a_transaction_and_makes_no_network_call() -> None:
    inside: list[bool] = []

    class Watching(InMemoryTransactionRunner):
        running = False

        async def run(self, work):  # type: ignore[no-untyped-def]
            self.running = True
            try:
                return await work()
            finally:
                self.running = False

    runner = Watching()
    events = RecordingEventPublisher()
    original = events.publish

    async def publish(batch):  # type: ignore[no-untyped-def]
        inside.append(runner.running)
        await original(batch)

    events.publish = publish  # type: ignore[method-assign]

    await RequestCheckoutClose(events=events, transactions=runner)(
        status_changed("pending_payment", "paid")
    )

    assert inside == [True]


# --- CloseCheckout -----------------------------------------------------------------------------


def closer(
    gateway: FakeGateway, checkouts: InMemoryCheckouts, clock: FixedClock | None = None
) -> CloseCheckout:
    return CloseCheckout(
        gateway=gateway,
        checkouts=checkouts,
        payments=InMemoryPayments(),
        transactions=InMemoryTransactionRunner(),
        clock=clock or FixedClock(NOW),
    )


async def test_it_expires_the_preference_now_and_closes_the_stored_checkout() -> None:
    gateway = FakeGateway()
    checkouts = InMemoryCheckouts(make_checkout(NOW + timedelta(hours=40), preference_id="pref-7"))

    await closer(gateway, checkouts)(close_requested())

    assert gateway.closed == [("pref-7", NOW)]
    assert checkouts.by_order[ORDER_ID].expires_at == NOW
    assert checkouts.by_order[ORDER_ID].preference_id == "pref-7"


async def test_an_order_without_a_checkout_calls_nothing() -> None:
    gateway = FakeGateway()
    checkouts = InMemoryCheckouts()

    await closer(gateway, checkouts)(close_requested())

    assert gateway.closed == []
    assert checkouts.by_order == {}


async def test_only_the_checkout_of_the_requested_order_is_closed() -> None:
    other = UUID(int=901)
    gateway = FakeGateway()
    own = make_checkout(NOW + timedelta(hours=1), preference_id="pref-own")
    foreign = make_checkout(NOW + timedelta(hours=1), preference_id="pref-other")
    foreign = type(foreign)(
        order_id=other,
        preference_id=foreign.preference_id,
        redirect_url=foreign.redirect_url,
        expires_at=foreign.expires_at,
        created_at=foreign.created_at,
    )
    checkouts = InMemoryCheckouts(own, foreign)

    await closer(gateway, checkouts)(close_requested())

    assert gateway.closed == [("pref-own", NOW)]
    assert checkouts.by_order[other].expires_at == NOW + timedelta(hours=1)


async def test_an_unknown_preference_is_logged_and_the_checkout_still_closed() -> None:
    gateway = FakeGateway()
    gateway.unknown_preferences.add("pref-gone")
    checkouts = InMemoryCheckouts(
        make_checkout(NOW + timedelta(hours=1), preference_id="pref-gone")
    )

    with capture_logs() as logs:
        await closer(gateway, checkouts)(close_requested())

    [log] = [entry for entry in logs if entry["event"] == "payments.checkout_missing"]
    assert (log["order_id"], log["preference_id"]) == (str(ORDER_ID), "pref-gone")
    assert checkouts.by_order[ORDER_ID].expires_at == NOW


async def test_a_gateway_failure_raises_so_the_relay_retries_and_the_checkout_stays_open() -> None:
    gateway = FakeGateway(fail=True)
    expires = NOW + timedelta(hours=1)
    checkouts = InMemoryCheckouts(make_checkout(expires))

    with pytest.raises(RuntimeError, match=r"^payments\.gateway_unavailable$"):
        await closer(gateway, checkouts)(close_requested())

    assert checkouts.by_order[ORDER_ID].expires_at == expires


async def test_closing_twice_is_harmless() -> None:
    gateway = FakeGateway()
    checkouts = InMemoryCheckouts(make_checkout(NOW + timedelta(hours=1)))
    handler = closer(gateway, checkouts)

    await handler(close_requested())
    await handler(close_requested())

    assert checkouts.by_order[ORDER_ID].expires_at == NOW
    assert len(gateway.closed) == 2
