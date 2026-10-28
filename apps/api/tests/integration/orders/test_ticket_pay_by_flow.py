"""Payments plan 003 step 11 (S1) on PostgreSQL: with two tickets for one order, the outbox
`pay_by` values are the first ticket's expiry and, for the second, the earlier of its own and the
first ticket's expiry; the order's deadline keeps the margin after the first expiry."""

from datetime import datetime, timedelta

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.test_stock_status_flow import outbox_rows
from tests.integration.orders.test_order_lifecycle_flow import order_row, place
from tests.integration.orders.test_payment_confirmation_flow import handler, notification
from tests.integration.orders.test_payment_hold_flow import (
    clean_payments,  # noqa: F401
    due_and_reservation,
    in_hours,
    pending_events,
    ticket,
)

pytestmark = pytest.mark.integration


async def two_tickets(
    container: Container, catalog_rows: CatalogRows, second_hours: float
) -> tuple[datetime, datetime, list[dict[str, datetime]]]:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    first = in_hours(48)
    second = in_hours(second_hours)
    gateway = FakeGateway(
        payments={
            "p1": ticket(placed, first, payment_id="p1", amount_cents=total),
            "p2": ticket(placed, second, payment_id="p2", amount_cents=total),
        }
    )
    process = handler(container, gateway)
    await process(notification("p1"))
    await process(notification("p2"))
    assert await due_and_reservation(container, placed) == (
        first + TICKET_GRACE,
        first + TICKET_GRACE,
    )
    events = [
        {
            "pay_by": datetime.fromisoformat(e["payload"]["pay_by"]),
            "due_at": datetime.fromisoformat(e["payload"]["due_at"]),
        }
        for e in sorted(pending_events(await outbox_rows(container)), key=lambda e: e["id"])
    ]
    return first, second, events


async def test_a_second_ticket_expiring_later_tells_the_first_expiry(
    container: Container, catalog_rows: CatalogRows
) -> None:
    first, _, events = await two_tickets(container, catalog_rows, 48 + 60)

    assert sorted(e["pay_by"] for e in events) == [first, first]
    assert {e["due_at"] for e in events} == {first + TICKET_GRACE}


async def test_a_second_ticket_expiring_earlier_tells_its_own_expiry(
    container: Container, catalog_rows: CatalogRows
) -> None:
    first, second, events = await two_tickets(container, catalog_rows, 20)

    assert second < first
    assert sorted(e["pay_by"] for e in events) == [second, first]
    # No margin-inside date is ever told: every `pay_by` leaves the deadline by the margin.
    assert all(e["due_at"] - e["pay_by"] >= TICKET_GRACE - timedelta(0) for e in events)
