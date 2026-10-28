"""Payments plan 003 step 10 (decision 22) on PostgreSQL: the order and its reservation wait for
the ticket's expiry plus `TICKET_GRACE`, the event tells the ticket's own `pay_by`, and a later
capped ticket tells `min(its expiry, deadline)`."""

from datetime import datetime

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.orders.application.commands.lifecycle import CancelOverdueOrders
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.test_stock_status_flow import outbox_rows
from tests.integration.orders.test_order_lifecycle_flow import order_row, place
from tests.integration.orders.test_payment_confirmation_flow import (
    handler,
    notification,
    payment_row,
)
from tests.integration.orders.test_payment_hold_flow import (
    clean_payments,  # noqa: F401
    due_and_reservation,
    in_hours,
    pending_events,
    ticket,
)

pytestmark = pytest.mark.integration


async def test_the_order_and_the_reservation_reach_the_expiry_plus_the_grace(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    expires = in_hours(48)
    gateway = FakeGateway(
        payments={"p1": ticket(placed, expires, payment_id="p1", amount_cents=total)}
    )

    await handler(container, gateway)(notification("p1"))

    assert await due_and_reservation(container, placed) == (
        expires + TICKET_GRACE,
        expires + TICKET_GRACE,
    )
    [event] = pending_events(await outbox_rows(container))
    assert datetime.fromisoformat(event["payload"]["pay_by"]) == expires
    assert datetime.fromisoformat(event["payload"]["due_at"]) == expires + TICKET_GRACE
    assert (await payment_row(container))["expires_at"] == expires
    # The overdue job leaves the order alone: it is not due until the margin ends.
    assert await container.services.get(CancelOverdueOrders).run_once() == 0


async def test_a_later_ticket_is_capped_and_tells_the_earlier_of_its_expiry_and_the_deadline(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    first = in_hours(48)
    far = in_hours(48 + 60)
    gateway = FakeGateway(
        payments={
            "p1": ticket(placed, first, payment_id="p1", amount_cents=total),
            "p2": ticket(placed, far, payment_id="p2", amount_cents=total),
        }
    )
    process = handler(container, gateway)

    await process(notification("p1"))
    await process(notification("p2"))

    cap = first + TICKET_GRACE
    assert await due_and_reservation(container, placed) == (cap, cap)
    events = pending_events(await outbox_rows(container))
    pay_bys = sorted(datetime.fromisoformat(e["payload"]["pay_by"]) for e in events)
    assert pay_bys == [first, cap]
