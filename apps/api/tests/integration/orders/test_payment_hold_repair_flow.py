"""Payments plan 003 step 8 (review repair) on PostgreSQL: the handler end to end with the cap at
the first hold (M1) and tickets that are expired when they arrive (L1), over the real facade,
inventory and outbox."""

from datetime import datetime

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
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
    due_and_reservation,
    in_hours,
    pending_events,
    ticket,
)
from tests.integration.orders.test_sql_orders import rows

pytestmark = pytest.mark.integration


async def test_a_second_ticket_is_capped_at_the_first_tickets_expiry(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    first = in_hours(48)
    later = in_hours(90)
    gateway = FakeGateway(
        payments={
            "first": ticket(placed, first, payment_id="first", amount_cents=total),
            "second": ticket(
                placed, later, payment_id="second", amount_cents=total, payment_method="clabe"
            ),
        }
    )
    process = handler(container, gateway)

    await process(notification("first"))
    await process(notification("second"))

    assert await due_and_reservation(container, placed) == (first, first)
    events = pending_events(await outbox_rows(container))
    assert {datetime.fromisoformat(e["payload"]["due_at"]) for e in events} == {first}
    assert len(events) == 2
    [stored] = await rows(
        container, "SELECT expires_at, held_at FROM payments.payments WHERE payment_id = 'second'"
    )
    assert stored["expires_at"] == later  # recorded as Mercado Pago reports it
    assert stored["held_at"] is not None


async def test_a_ticket_expired_on_arrival_holds_nothing_and_sends_no_email(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    before = await due_and_reservation(container, placed)
    gateway = FakeGateway(payments={PAYMENT_ID: ticket(placed, in_hours(-1), amount_cents=total)})
    process = handler(container, gateway)

    await process(notification())
    await process(notification())

    assert (await payment_row(container))["held_at"] is not None
    assert await due_and_reservation(container, placed) == before
    assert pending_events(await outbox_rows(container)) == []


async def test_an_expired_on_arrival_ticket_does_not_cap_the_next_real_one(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    real = in_hours(48)
    gateway = FakeGateway(
        payments={
            "gone": ticket(placed, in_hours(-1), payment_id="gone", amount_cents=total),
            "real": ticket(placed, real, payment_id="real", amount_cents=total),
        }
    )
    process = handler(container, gateway)

    await process(notification("gone"))
    await process(notification("real"))

    assert await due_and_reservation(container, placed) == (real, real)
    assert len(pending_events(await outbox_rows(container))) == 1
