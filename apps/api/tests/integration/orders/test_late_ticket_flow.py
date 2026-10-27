"""Payments plan 003 step 9 (R2) on PostgreSQL: a pending ticket notified after its order was
cancelled is marked held, publishes `payments.ticket.cancel_requested` through the real outbox,
and `CancelTicket` then cancels it in the (fake) gateway."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.orders.application.commands.lifecycle import CancelUnpaidOrder
from fragancia_api.modules.payments.application.handlers.cancel_ticket import CancelTicket
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.test_stock_status_flow import outbox_rows
from tests.integration.orders.test_order_lifecycle_flow import order_row, place
from tests.integration.orders.test_payment_confirmation_flow import (
    PAYMENT_ID,
    handler,
    notification,
    payment_row,
)
from tests.integration.orders.test_payment_hold_flow import (
    clean_payments,  # noqa: F401
    in_hours,
    pending_events,
    ticket,
)

pytestmark = pytest.mark.integration

CANCEL_REQUESTED = "payments.ticket.cancel_requested"


async def test_a_ticket_notified_after_the_order_was_cancelled_is_cancelled_in_mercado_pago(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    cancelled = await container.services.get(CancelUnpaidOrder).execute(
        placed.number, "Motivo", "owner-1"
    )
    assert isinstance(cancelled, Ok)
    gateway = FakeGateway(payments={PAYMENT_ID: ticket(placed, in_hours(48), amount_cents=total)})
    process = handler(container, gateway)

    await process(notification())
    await process(notification())

    stored = await payment_row(container)
    assert stored["held_at"] is not None
    assert pending_events(await outbox_rows(container)) == []
    requests = [r for r in await outbox_rows(container) if r["name"] == CANCEL_REQUESTED]
    assert [r["payload"] for r in requests] == [{"payment_id": PAYMENT_ID}]

    await CancelTicket(gateway=gateway)(
        EventMessage(
            id=UUID(int=8),
            name=CANCEL_REQUESTED,
            payload=requests[0]["payload"],
            occurred_at=datetime(2026, 10, 27, tzinfo=UTC),
        )
    )

    assert gateway.cancelled == [PAYMENT_ID]
    assert (await order_row(container, placed.number))["status"] == "cancelled"


async def test_a_ticket_for_a_pending_order_publishes_no_cancel_request(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(payments={PAYMENT_ID: ticket(placed, in_hours(48), amount_cents=total)})

    await handler(container, gateway)(notification())

    assert [r for r in await outbox_rows(container) if r["name"] == CANCEL_REQUESTED] == []
