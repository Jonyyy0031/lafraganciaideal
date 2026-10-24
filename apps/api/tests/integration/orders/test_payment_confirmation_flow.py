"""Payments plan 002 on PostgreSQL (`fragancia_test`): `OrderPaymentConfirmations` over the real
orders adapters and the real inventory (commit and release in the same transaction as the move),
the lock of `get_by_id_for_update` under concurrent payments, and the payments handler end to end
(real `SqlPaymentRepository`, real facade, a fake Mercado Pago): applied once, redelivery a no-op,
the stock-gone and mismatch flags in the outbox, and the rollback when orders refuses."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.inventory.application.commands.reservations import ExpireReservations
from fragancia_api.modules.orders import OrderPaymentConfirmations, OrderPayments
from fragancia_api.modules.orders.application.commands.confirm_payment import (
    PaymentConfirmation,
    PaymentConfirmationOutcome,
)
from fragancia_api.modules.orders.application.commands.lifecycle import (
    CancelUnpaidOrder,
    MarkOrderPaid,
)
from fragancia_api.modules.orders.infrastructure.sql_order_repository import SqlOrderRepository
from fragancia_api.modules.payments.application.handlers.process_payment import (
    ProcessPaymentNotification,
)
from fragancia_api.modules.payments.application.ports import GatewayPayment
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.modules.payments.infrastructure.order_payments import OrderPaymentsAdapter
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import (
    SqlPaymentRepository,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher
from fragancia_api.shared.kernel import DomainError, Ok, Result
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import (
    item_row,
    make_due,
    reservation_row,
    table_count,
)
from tests.integration.inventory.test_stock_status_flow import lose, outbox_rows
from tests.integration.orders.test_order_lifecycle_flow import (
    Placed,
    history,
    movements,
    order_row,
    place,
    status_events,
)
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import row, rows

pytestmark = pytest.mark.integration

PAYMENT_ID = "555000111"
OWNER = "owner-1"


@pytest.fixture(autouse=True)
async def clean_payments(container: Container) -> AsyncIterator[None]:
    async def reset() -> None:
        async with container.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE payments.checkouts, payments.payments"))

    await reset()
    yield
    await reset()


async def confirm(
    container: Container,
    placed: Placed,
    *,
    amount: int | None = None,
    currency: str = "MXN",
    payment_id: str = PAYMENT_ID,
) -> Result[PaymentConfirmation, DomainError]:
    stored = await order_row(container, placed.number)
    return await container.services.get(OrderPaymentConfirmations).confirm(
        placed.order_id, payment_id, stored["total_cents"] if amount is None else amount, currency
    )


def outcome_of(result: Result[PaymentConfirmation, DomainError]) -> PaymentConfirmationOutcome:
    assert isinstance(result, Ok), result
    return result.value.outcome


# --- the facade --------------------------------------------------------------------------------


async def test_a_matching_payment_commits_the_reservation_and_pays_in_one_transaction(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    assert await item_row(container, placed.stock) == (5, 2)

    result = await confirm(container, placed)

    assert outcome_of(result) is PaymentConfirmationOutcome.PAID
    assert (await order_row(container, placed.number))["status"] == "paid"
    assert (await reservation_row(container, placed.reservation_id))["status"] == "committed"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (3, 0)
    [sale] = await movements(container, placed.stock, "sale")
    assert (sale["quantity"], sale["delta"]) == (2, -2)
    [event] = await history(container, placed.number)
    assert (event["from_status"], event["to_status"], event["actor"]) == (
        "pending_payment",
        "paid",
        "system",
    )
    assert event["note"] == f"Pagado con Mercado Pago (pago {PAYMENT_ID})"
    [status_event] = await status_events(container)
    assert status_event["payload"]["to_status"] == "paid"
    paid_rows = [r for r in await outbox_rows(container) if r["name"] == "orders.order.paid"]
    assert len(paid_rows) == 1


async def test_a_payment_after_the_reservation_expired_pays_while_the_units_are_there(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1

    result = await confirm(container, placed)

    assert outcome_of(result) is PaymentConfirmationOutcome.PAID
    assert (await order_row(container, placed.number))["status"] == "paid"
    assert await item_row(container, placed.stock) == (3, 0)


async def test_units_that_are_gone_cancel_the_order_and_release_the_reservation(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, units=2, quantity=2)
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1
    await lose(container, placed.stock, 2)
    sales_before = await table_count(container, "stock_movements")

    result = await confirm(container, placed)

    assert outcome_of(result) is PaymentConfirmationOutcome.STOCK_GONE
    stored = await order_row(container, placed.number)
    assert stored["status"] == "cancelled"
    assert stored["cancel_reason"] == f"Pago recibido sin existencias (pago {PAYMENT_ID})"
    [event] = await history(container, placed.number)
    assert (event["from_status"], event["to_status"], event["actor"]) == (
        "pending_payment",
        "cancelled",
        "system",
    )
    assert await movements(container, placed.stock, "sale") == []
    assert await table_count(container, "stock_movements") == sales_before
    assert [e["payload"]["to_status"] for e in await status_events(container)] == ["cancelled"]


async def test_units_that_a_loss_left_short_under_a_live_reservation_also_cancel(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, units=2, quantity=2)
    await lose(container, placed.stock, 1)

    result = await confirm(container, placed)

    assert outcome_of(result) is PaymentConfirmationOutcome.STOCK_GONE
    assert (await order_row(container, placed.number))["status"] == "cancelled"
    assert (await reservation_row(container, placed.reservation_id))["status"] != "committed"  # type: ignore[arg-type]
    assert await movements(container, placed.stock, "sale") == []


async def test_another_amount_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]

    wrong_amount = await confirm(container, placed, amount=total - 1)
    wrong_currency = await confirm(container, placed, currency="USD")

    assert outcome_of(wrong_amount) is PaymentConfirmationOutcome.AMOUNT_MISMATCH
    assert outcome_of(wrong_currency) is PaymentConfirmationOutcome.AMOUNT_MISMATCH
    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    assert await history(container, placed.number) == []
    assert await status_events(container) == []
    assert await item_row(container, placed.stock) == (5, 2)


async def test_an_unknown_order_id_is_not_found(container: Container) -> None:
    result = await container.services.get(OrderPaymentConfirmations).confirm(
        UUID(int=424242), PAYMENT_ID, 100, "MXN"
    )

    assert outcome_of(result) is PaymentConfirmationOutcome.NOT_FOUND


async def test_a_cancelled_order_is_not_pending_and_keeps_its_reservation_released(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    cancelled = await container.services.get(CancelUnpaidOrder).execute(
        placed.number, "Motivo", OWNER
    )
    assert isinstance(cancelled, Ok)
    events_before = len(await history(container, placed.number))

    result = await confirm(container, placed)

    assert isinstance(result, Ok)
    assert result.value.outcome is PaymentConfirmationOutcome.NOT_PENDING
    assert result.value.status == "cancelled"
    assert len(await history(container, placed.number)) == events_before
    assert await movements(container, placed.stock, "sale") == []


async def test_a_second_payment_for_an_order_paid_by_hand_is_not_pending(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    paid = await container.services.get(MarkOrderPaid).execute(placed.number, "Efectivo", OWNER)
    assert isinstance(paid, Ok)

    result = await confirm(container, placed)

    assert outcome_of(result) is PaymentConfirmationOutcome.NOT_PENDING
    assert len(await movements(container, placed.stock, "sale")) == 1
    assert len(await history(container, placed.number)) == 1


async def test_an_order_without_a_reservation_is_paid_without_touching_inventory(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, made_only=True)
    assert placed.reservation_id is None
    ledger_before = await table_count(container, "stock_movements")

    result = await confirm(container, placed)

    assert outcome_of(result) is PaymentConfirmationOutcome.PAID
    assert (await order_row(container, placed.number))["status"] == "paid"
    assert await table_count(container, "stock_movements") == ledger_before
    assert await table_count(container, "reservations") == 0


async def test_two_payments_at_once_sell_the_units_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    facade = container.services.get(OrderPaymentConfirmations)

    results = await asyncio.wait_for(
        asyncio.gather(
            facade.confirm(placed.order_id, "111", total, "MXN"),
            facade.confirm(placed.order_id, "222", total, "MXN"),
        ),
        20,
    )

    assert sorted(outcome_of(r) for r in results) == [
        PaymentConfirmationOutcome.NOT_PENDING,
        PaymentConfirmationOutcome.PAID,
    ]
    assert len(await movements(container, placed.stock, "sale")) == 1
    assert await item_row(container, placed.stock) == (3, 0)
    assert len(await history(container, placed.number)) == 1
    assert len(await status_events(container)) == 1


async def test_a_manual_payment_and_a_webhook_payment_at_once_pay_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]

    manual, automatic = await asyncio.wait_for(
        asyncio.gather(
            container.services.get(MarkOrderPaid).execute(placed.number, "Efectivo", OWNER),
            container.services.get(OrderPaymentConfirmations).confirm(
                placed.order_id, PAYMENT_ID, total, "MXN"
            ),
        ),
        20,
    )

    assert isinstance(automatic, Ok)
    paid_by_one = [
        isinstance(manual, Ok),
        automatic.value.outcome is PaymentConfirmationOutcome.PAID,
    ]
    assert sorted(paid_by_one) == [False, True]
    assert len(await movements(container, placed.stock, "sale")) == 1
    assert len(await history(container, placed.number)) == 1


async def test_get_by_id_for_update_finds_the_order_and_none_for_an_unknown_id(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    repo = SqlOrderRepository(container.database)

    found = await in_transaction(container, lambda: repo.get_by_id_for_update(placed.order_id))
    missing = await in_transaction(container, lambda: repo.get_by_id_for_update(UUID(int=1)))

    assert found is not None
    assert (found.id, found.number, found.reservation_id) == (
        placed.order_id,
        placed.number,
        placed.reservation_id,
    )
    assert [line.quantity for line in found.lines] == [2, 1]
    assert missing is None


async def test_get_by_id_for_update_locks_the_order_against_a_concurrent_payment(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    repo = SqlOrderRepository(container.database)
    holds = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def holder() -> Result[None, DomainError]:
        assert await repo.get_by_id_for_update(placed.order_id) is not None
        holds.set()
        await asyncio.wait_for(release.wait(), 10)
        seen.append("holder done")
        return Ok(None)

    async def payer() -> Result[None, DomainError]:
        seen.append("payer asks")
        result = await confirm(container, placed)
        seen.append("payer done")
        assert isinstance(result, Ok)
        return Ok(None)

    runner = SqlTransactionRunner(container.database)
    first = asyncio.create_task(runner.run(holder))
    await asyncio.wait_for(holds.wait(), 10)
    second = asyncio.create_task(payer())
    await asyncio.sleep(0.5)
    assert seen == ["payer asks"]  # the payment waits behind the lock
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 15)

    assert seen == ["payer asks", "holder done", "payer done"]
    assert (await order_row(container, placed.number))["status"] == "paid"


# --- the payments handler, end to end ----------------------------------------------------------


def gateway_payment(placed: Placed, **overrides: object) -> GatewayPayment:
    values: dict[str, object] = {
        "payment_id": PAYMENT_ID,
        "status": "approved",
        "status_detail": "accredited",
        "external_reference": str(placed.order_id),
        "amount_cents": 0,
        "currency": "MXN",
        "payment_type": "credit_card",
        "approved_at": datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return GatewayPayment(**values)  # type: ignore[arg-type]


def handler(container: Container, gateway: FakeGateway) -> ProcessPaymentNotification:
    adapter = OrderPaymentsAdapter(
        container.services.get(OrderPayments),
        container.services.get(OrderPaymentConfirmations),
    )
    return ProcessPaymentNotification(
        gateway=gateway,
        payments=SqlPaymentRepository(container.database),
        orders=adapter,
        transactions=SqlTransactionRunner(container.database),
        events=OutboxEventPublisher(container.database),
        clock=SystemClock(),
    )


def notification(payment_id: str = PAYMENT_ID) -> EventMessage:
    return EventMessage(
        id=UUID(int=1),
        name="payments.notification.received",
        payload={"payment_id": payment_id},
        occurred_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
    )


async def payment_row(container: Container) -> dict[str, object]:
    return await row(container, "SELECT * FROM payments.payments")


def flagged_rows(rows_: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows_ if r["name"] == "payments.payment.flagged"]


async def test_an_approved_payment_pays_the_order_and_is_stored_applied(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(placed, amount_cents=total)})

    await handler(container, gateway)(notification())

    assert (await order_row(container, placed.number))["status"] == "paid"
    stored = await payment_row(container)
    assert (stored["outcome"], stored["status"], stored["order_id"]) == (
        "applied",
        "approved",
        placed.order_id,
    )
    assert (stored["amount_cents"], stored["currency"]) == (total, "MXN")
    assert flagged_rows(await outbox_rows(container)) == []


async def test_a_redelivered_notification_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(placed, amount_cents=total)})
    process = handler(container, gateway)
    await process(notification())
    outbox_before = len(await outbox_rows(container))
    order_before = await order_row(container, placed.number)

    await process(notification())
    await process(notification())

    assert await order_row(container, placed.number) == order_before
    assert len(await outbox_rows(container)) == outbox_before
    assert len(await history(container, placed.number)) == 1
    assert len(await movements(container, placed.stock, "sale")) == 1
    assert (await payment_row(container))["outcome"] == "applied"


async def test_a_rejected_payment_is_stored_and_the_order_stays_pending(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    rejected = gateway_payment(
        placed, status="rejected", status_detail="cc_rejected_other_reason", amount_cents=total
    )

    await handler(container, FakeGateway(payments={PAYMENT_ID: rejected}))(notification())

    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    stored = await payment_row(container)
    assert (stored["status"], stored["outcome"]) == ("rejected", None)
    assert await item_row(container, placed.stock) == (5, 2)


async def test_a_pending_payment_that_is_later_approved_is_applied_on_the_second_delivery(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(
        payments={PAYMENT_ID: gateway_payment(placed, status="in_process", amount_cents=total)}
    )
    process = handler(container, gateway)
    await process(notification())
    assert (await order_row(container, placed.number))["status"] == "pending_payment"

    gateway.payments[PAYMENT_ID] = gateway_payment(placed, amount_cents=total)
    await process(notification())

    assert (await order_row(container, placed.number))["status"] == "paid"
    stored = await payment_row(container)
    assert (stored["status"], stored["outcome"]) == ("approved", "applied")


async def test_an_approved_payment_for_a_cancelled_order_is_refund_due_and_not_flagged(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    cancelled = await container.services.get(CancelUnpaidOrder).execute(
        placed.number, "Motivo", OWNER
    )
    assert isinstance(cancelled, Ok)
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(placed, amount_cents=total)})

    await handler(container, gateway)(notification())

    assert (await order_row(container, placed.number))["status"] == "cancelled"
    assert (await payment_row(container))["outcome"] == "refund_due"
    assert flagged_rows(await outbox_rows(container)) == []


async def test_a_second_approved_payment_for_a_paid_order_is_refund_due(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    gateway = FakeGateway(
        payments={
            "111": gateway_payment(placed, payment_id="111", amount_cents=total),
            "222": gateway_payment(placed, payment_id="222", amount_cents=total),
        }
    )
    process = handler(container, gateway)

    await process(notification("111"))
    await process(notification("222"))

    stored = {
        r["payment_id"]: r["outcome"]
        for r in await rows(container, "SELECT payment_id, outcome FROM payments.payments")
    }
    assert stored == {"111": "applied", "222": "refund_due"}
    assert len(await movements(container, placed.stock, "sale")) == 1


async def test_a_payment_with_another_amount_is_flagged_and_the_order_stays_pending(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(placed, amount_cents=100)})

    await handler(container, gateway)(notification())

    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    assert (await payment_row(container))["outcome"] == "amount_mismatch"
    [flag] = flagged_rows(await outbox_rows(container))
    assert flag["payload"] == {
        "payment_id": PAYMENT_ID,
        "number": placed.number,
        "reason": "amount_mismatch",
        "amount_cents": 100,
        "currency": "MXN",
    }


async def test_a_payment_when_the_units_are_gone_cancels_the_order_and_is_flagged(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, units=2, quantity=2)
    total = (await order_row(container, placed.number))["total_cents"]
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1
    await lose(container, placed.stock, 2)
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(placed, amount_cents=total)})

    await handler(container, gateway)(notification())

    assert (await order_row(container, placed.number))["status"] == "cancelled"
    assert (await payment_row(container))["outcome"] == "refund_due"
    [flag] = flagged_rows(await outbox_rows(container))
    assert flag["payload"]["reason"] == "stock_gone"
    assert flag["payload"]["number"] == placed.number


async def test_a_payment_for_an_order_id_that_does_not_exist_keeps_its_outcome_unset(
    container: Container,
) -> None:
    ghost = Placed(
        number=9999, order_id=UUID(int=777), stock=UUID(int=1), made=None, reservation_id=None
    )
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(ghost, amount_cents=100)})

    await handler(container, gateway)(notification())

    stored = await payment_row(container)
    assert (stored["status"], stored["outcome"]) == ("approved", None)
    assert flagged_rows(await outbox_rows(container)) == []


async def test_a_gateway_failure_raises_and_stores_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    gateway = FakeGateway(fail=True)

    with pytest.raises(RuntimeError, match="payments.gateway_unavailable"):
        await handler(container, gateway)(notification())

    assert await table_count_payments(container) == 0
    assert (await order_row(container, placed.number))["status"] == "pending_payment"


async def test_a_refusal_from_inventory_rolls_back_the_payment_row_and_the_order(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    total = (await order_row(container, placed.number))["total_cents"]
    # A returned reservation is neither committable nor releasable: the facade passes the
    # refusal through and the handler must leave no half-applied state.
    async with container.database.engine.begin() as connection:
        found = (
            await connection.execute(
                text("SELECT count(*) FROM inventory.reservations WHERE id = :r"),
                {"r": placed.reservation_id},
            )
        ).scalar_one()
        assert found == 1  # proves the WHERE before the UPDATE
        await connection.execute(
            text("UPDATE inventory.reservations SET status = 'returned' WHERE id = :r"),
            {"r": placed.reservation_id},
        )
    gateway = FakeGateway(payments={PAYMENT_ID: gateway_payment(placed, amount_cents=total)})

    with pytest.raises(RuntimeError, match=r"payments\.confirm_refused: INVENTORY_"):
        await handler(container, gateway)(notification())

    assert await table_count_payments(container) == 0
    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    assert await history(container, placed.number) == []
    assert await status_events(container) == []


async def table_count_payments(container: Container) -> int:
    found = await row(container, "SELECT count(*) AS n FROM payments.payments")
    return int(found["n"])
