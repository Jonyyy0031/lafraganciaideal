"""Payments plan 003 on PostgreSQL (`fragancia_test`): `OrderPaymentConfirmations.hold_for_payment`
over the real orders adapters and the real inventory `extend` in one transaction (the saved
`payment_due_at`, the reservation's `expires_at`, the outbox event), a reservation that already
expired, the overdue job at the extended deadline, and the payments handler end to end with a
pending OXXO or SPEI ticket (stored columns, once per payment, then approved)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.inventory.application.commands.reservations import ExpireReservations
from fragancia_api.modules.orders import OrderPaymentConfirmations
from fragancia_api.modules.orders.application.commands.confirm_payment import (
    PaymentConfirmation,
    PaymentConfirmationOutcome,
)
from fragancia_api.modules.orders.application.commands.lifecycle import (
    CancelOverdueOrders,
    CancelUnpaidOrder,
)
from fragancia_api.modules.payments.application.ports import GatewayPayment
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.shared.kernel import DomainError, Ok, Result
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import item_row, make_due, reservation_row
from tests.integration.inventory.test_stock_status_flow import outbox_rows
from tests.integration.orders.test_order_lifecycle_flow import (
    Placed,
    backdate_due,
    history,
    movements,
    order_row,
    place,
    status_events,
)
from tests.integration.orders.test_payment_confirmation_flow import (
    PAYMENT_ID,
    gateway_payment,
    handler,
    notification,
    payment_row,
)
from tests.integration.orders.test_sql_orders import rows

pytestmark = pytest.mark.integration

PENDING_EVENT = "orders.order.payment_pending"
URL = "https://mp.test/ticket/555000111"


@pytest.fixture(autouse=True)
async def clean_payments(container: Container) -> AsyncIterator[None]:
    async def reset() -> None:
        async with container.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE payments.checkouts, payments.payments"))

    await reset()
    yield
    await reset()


def in_hours(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


def unwrapped(result: Result[PaymentConfirmation, DomainError]) -> PaymentConfirmation:
    assert isinstance(result, Ok), result
    return result.value


async def hold(
    container: Container,
    placed: Placed,
    until: datetime,
    method: str = "efectivo en tienda",
    order_id: UUID | None = None,
) -> PaymentConfirmation:
    facade = container.services.get(OrderPaymentConfirmations)
    return unwrapped(
        await facade.hold_for_payment(order_id or placed.order_id, until, until, method, URL)
    )


async def due_and_reservation(container: Container, placed: Placed) -> tuple[datetime, datetime]:
    order = await order_row(container, placed.number)
    reservation = await reservation_row(container, placed.reservation_id)  # type: ignore[arg-type]
    return order["payment_due_at"], reservation["expires_at"]


def pending_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["name"] == PENDING_EVENT]


# --- the facade --------------------------------------------------------------------------------


async def test_a_ticket_moves_the_saved_deadline_and_the_reservation_to_its_expiry(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    due_before, expires_before = await due_and_reservation(container, placed)
    assert due_before == expires_before
    until = in_hours(48)

    result = await hold(container, placed, until)

    assert result.outcome is PaymentConfirmationOutcome.HELD
    assert await due_and_reservation(container, placed) == (until, until)
    stored = await order_row(container, placed.number)
    assert stored["status"] == "pending_payment"
    assert (await reservation_row(container, placed.reservation_id))["status"] == "active"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (5, 2)  # the units are still held
    assert await history(container, placed.number) == []
    assert await status_events(container) == []


async def test_the_event_lands_in_the_outbox_with_the_new_deadline_and_the_page(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    until = in_hours(48)

    await hold(container, placed, until, "transferencia SPEI")

    [event] = pending_events(await outbox_rows(container))
    payload = event["payload"]
    assert payload["order_id"] == str(placed.order_id)
    assert payload["number"] == placed.number
    assert payload["method"] == "transferencia SPEI"
    assert payload["instructions_url"] == URL
    assert datetime.fromisoformat(payload["due_at"]) == until


async def test_a_ticket_expiring_before_the_deadline_shortens_nothing_and_still_publishes(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await hold(container, placed, in_hours(48))
    moved = await due_and_reservation(container, placed)

    result = await hold(container, placed, in_hours(5))

    assert result.outcome is PaymentConfirmationOutcome.HELD
    assert await due_and_reservation(container, placed) == moved
    assert len(pending_events(await outbox_rows(container))) == 2


async def test_a_second_ticket_expiring_later_moves_the_deadline_again(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await hold(container, placed, in_hours(40))
    later = in_hours(47)

    await hold(container, placed, later)

    assert await due_and_reservation(container, placed) == (later, later)


async def test_an_expired_reservation_does_not_undo_the_deadline_and_the_ticket_pays_late(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1
    until = in_hours(48)

    result = await hold(container, placed, until)

    assert result.outcome is PaymentConfirmationOutcome.HELD
    stored = await order_row(container, placed.number)
    assert stored["payment_due_at"] == until
    assert (await reservation_row(container, placed.reservation_id))["status"] == "expired"  # type: ignore[arg-type]
    assert len(pending_events(await outbox_rows(container))) == 1
    paid = unwrapped(
        await container.services.get(OrderPaymentConfirmations).confirm(
            placed.order_id, PAYMENT_ID, stored["total_cents"], "MXN"
        )
    )
    assert paid.outcome is PaymentConfirmationOutcome.PAID
    assert len(await movements(container, placed.stock, "sale")) == 1


async def test_a_cancelled_order_is_not_pending_and_nothing_is_saved_or_published(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    cancelled = await container.services.get(CancelUnpaidOrder).execute(
        placed.number, "Motivo", "owner-1"
    )
    assert isinstance(cancelled, Ok)
    due_before = (await order_row(container, placed.number))["payment_due_at"]

    result = await hold(container, placed, in_hours(48))

    assert result.outcome is PaymentConfirmationOutcome.NOT_PENDING
    assert result.status == "cancelled"
    assert (await order_row(container, placed.number))["payment_due_at"] == due_before
    assert pending_events(await outbox_rows(container)) == []


async def test_an_unknown_order_is_not_found(container: Container) -> None:
    result = unwrapped(
        await container.services.get(OrderPaymentConfirmations).hold_for_payment(
            UUID(int=424242), in_hours(48), in_hours(48), "efectivo en tienda", None
        )
    )

    assert result.outcome is PaymentConfirmationOutcome.NOT_FOUND
    assert pending_events(await outbox_rows(container)) == []


async def test_the_overdue_job_leaves_a_held_order_alone_until_the_extended_deadline(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await hold(container, placed, in_hours(48))
    job = container.services.get(CancelOverdueOrders)

    assert await job.run_once() == 0
    assert (await order_row(container, placed.number))["status"] == "pending_payment"

    await backdate_due(container, placed.number)

    assert await job.run_once() == 1
    assert (await order_row(container, placed.number))["status"] == "cancelled"
    assert (await reservation_row(container, placed.reservation_id))["status"] == "released"  # type: ignore[arg-type]


# --- the payments handler, end to end ----------------------------------------------------------


def ticket(placed: Placed, expires: datetime, **overrides: object) -> GatewayPayment:
    values: dict[str, object] = {
        "status": "pending",
        "status_detail": "pending_waiting_payment",
        "approved_at": None,
        "payment_type": "ticket",
        "payment_method": "oxxo",
        "expires_at": expires,
        "ticket_url": URL,
        "amount_cents": 0,
    }
    values.update(overrides)
    return gateway_payment(placed, **values)


async def test_a_pending_oxxo_payment_holds_the_order_and_stores_the_ticket_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    expires = in_hours(48)
    gateway = FakeGateway(payments={PAYMENT_ID: ticket(placed, expires, amount_cents=total)})
    process = handler(container, gateway)

    await process(notification())
    await process(notification())
    await process(notification())

    stored = await payment_row(container)
    assert (stored["status"], stored["outcome"], stored["payment_type"]) == (
        "pending",
        None,
        "ticket",
    )
    assert (stored["payment_method"], stored["expires_at"], stored["ticket_url"]) == (
        "oxxo",
        expires,
        URL,
    )
    assert stored["held_at"] is not None
    # Decision 22: the order and its reservation wait 48 h more than the ticket.
    held_until = expires + TICKET_GRACE
    assert await due_and_reservation(container, placed) == (held_until, held_until)
    assert len(pending_events(await outbox_rows(container))) == 1
    assert (await order_row(container, placed.number))["status"] == "pending_payment"


async def test_a_pending_spei_payment_holds_the_order_with_its_label(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(
        payments={
            PAYMENT_ID: ticket(
                placed,
                in_hours(48),
                amount_cents=total,
                payment_type="bank_transfer",
                payment_method="clabe",
            )
        }
    )

    await handler(container, gateway)(notification())

    [event] = pending_events(await outbox_rows(container))
    assert event["payload"]["method"] == "transferencia SPEI"


async def test_the_ticket_paid_later_pays_the_order_and_commits_the_reservation(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    expires = in_hours(48)
    gateway = FakeGateway(payments={PAYMENT_ID: ticket(placed, expires, amount_cents=total)})
    process = handler(container, gateway)
    await process(notification())
    held_at = (await payment_row(container))["held_at"]

    gateway.payments[PAYMENT_ID] = ticket(
        placed,
        expires,
        amount_cents=total,
        status="approved",
        status_detail="accredited",
        approved_at=datetime.now(UTC),
    )
    await process(notification())

    assert (await order_row(container, placed.number))["status"] == "paid"
    assert (await reservation_row(container, placed.reservation_id))["status"] == "committed"  # type: ignore[arg-type]
    stored = await payment_row(container)
    assert (stored["outcome"], stored["held_at"]) == ("applied", held_at)
    assert len(pending_events(await outbox_rows(container))) == 1


async def test_a_ticket_paid_after_a_card_payment_is_refund_due_and_the_order_stays_paid(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(
        payments={
            "card-1": gateway_payment(placed, payment_id="card-1", amount_cents=total),
            "oxxo-1": ticket(
                placed,
                in_hours(48),
                payment_id="oxxo-1",
                amount_cents=total,
                status="approved",
                approved_at=datetime.now(UTC),
            ),
        }
    )
    process = handler(container, gateway)

    await process(notification("card-1"))
    await process(notification("oxxo-1"))

    assert (await order_row(container, placed.number))["status"] == "paid"
    outcomes = {
        r["payment_id"]: r["outcome"]
        for r in await rows(container, "SELECT payment_id, outcome FROM payments.payments")
    }
    assert outcomes == {"card-1": "applied", "oxxo-1": "refund_due"}
    assert len(await movements(container, placed.stock, "sale")) == 1


async def test_a_ticket_for_a_cancelled_order_is_marked_held_and_sends_no_instructions(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    cancelled = await container.services.get(CancelUnpaidOrder).execute(
        placed.number, "Motivo", "owner-1"
    )
    assert cancelled.__class__.__name__ == "Ok"
    gateway = FakeGateway(payments={PAYMENT_ID: ticket(placed, in_hours(48), amount_cents=total)})
    process = handler(container, gateway)

    await process(notification())
    await process(notification())

    assert (await payment_row(container))["held_at"] is not None
    assert pending_events(await outbox_rows(container)) == []
    assert (await order_row(container, placed.number))["status"] == "cancelled"
