"""Plan 004 end to end on PostgreSQL (`fragancia_test`): the lifecycle commands over the real
orders adapters and the real inventory (commit, release and return movements, `on_hand` and
`reserved`), the rollback when inventory refuses, the outbox event of every move, concurrent
payments, and the overdue job (including deviation 5, a refused order skipped over real SQL)."""

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from structlog.testing import capture_logs

from fragancia_api.container import Container
from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.inventory.application.commands.reservations import ExpireReservations
from fragancia_api.modules.orders.application.commands.lifecycle import (
    AdvanceOrder,
    CancelOverdueOrders,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderPaid,
    MarkOrderRefunded,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import (
    item_row,
    make_due,
    reservation_row,
    table_count,
)
from tests.integration.inventory.test_stock_status_flow import lose, outbox_rows, restock
from tests.integration.orders.test_checkout_flow import (
    MADE_TO_ORDER,
    add_to_cart,
    configure_delivery,
    real_place_order,
    rows,
)
from tests.unit.orders.test_checkout import request

pytestmark = pytest.mark.integration

OWNER = "owner-1"
STATUS_CHANGED = "orders.order.status_changed"


@dataclass
class Placed:
    number: int
    order_id: UUID
    stock: UUID
    made: UUID | None
    reservation_id: UUID | None


def refused(result: Result[None, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


def ok(result: Result[None, DomainError]) -> None:
    assert isinstance(result, Ok), result


async def place(
    container: Container,
    catalog_rows: CatalogRows,
    *,
    units: int = 5,
    quantity: int = 2,
    method: str = "home",
    made_only: bool = False,
    name: str = "Flow",
) -> Placed:
    """One order through the real checkout: `quantity` of an in-stock presentation with `units` on
    hand (plus one made-to-order unit, or only that when `made_only`)."""
    await configure_delivery(container)
    _, [stock, made] = await catalog_rows.perfume(
        name, sizes=({"stock_available": units}, MADE_TO_ORDER)
    )
    await restock(container, stock, units)
    lines = [(made, 1)] if made_only else [(stock, quantity), (made, 1)]
    subtotal = 100_000 if made_only else quantity * 250_000 + 100_000
    token = await add_to_cart(container, lines)
    total = subtotal + (18_000 if method == "home" else 0)
    result = await real_place_order(container).execute(token, request(method, total))
    assert isinstance(result, Ok), result
    number = int(result.value.number.removeprefix("LFI-"))
    [order] = await rows(
        container, "SELECT id, reservation_id FROM orders.orders WHERE number = :n", n=number
    )
    return Placed(number, order["id"], stock, made, order["reservation_id"])


async def order_row(container: Container, number: int) -> dict[str, Any]:
    [found] = await rows(container, "SELECT * FROM orders.orders WHERE number = :n", n=number)
    return found


async def history(container: Container, number: int) -> list[dict[str, Any]]:
    return await rows(
        container,
        "SELECT e.* FROM orders.order_events e JOIN orders.orders o ON o.id = e.order_id "
        "WHERE o.number = :n ORDER BY e.at, e.id",
        n=number,
    )


async def movements(container: Container, presentation: UUID, kind: str) -> list[dict[str, Any]]:
    return await rows(
        container,
        "SELECT kind, quantity, delta, on_hand_after, reason, actor_id "
        "FROM inventory.stock_movements WHERE presentation_id = :p AND kind = :k "
        "ORDER BY created_at",
        p=presentation,
        k=kind,
    )


async def status_events(container: Container) -> list[dict[str, Any]]:
    return [r for r in await outbox_rows(container) if r["name"] == STATUS_CHANGED]


async def pay(container: Container, placed: Placed, note: str = "Transferencia directa") -> None:
    ok(await container.services.get(MarkOrderPaid).execute(placed.number, note, OWNER))


async def backdate_due(container: Container, number: int) -> None:
    async with container.database.engine.begin() as connection:
        found = (
            await connection.execute(
                text("SELECT count(*) FROM orders.orders WHERE number = :n"), {"n": number}
            )
        ).scalar_one()
        assert found == 1  # proves the WHERE before the UPDATE
        await connection.execute(
            text(
                "UPDATE orders.orders SET payment_due_at = now() - interval '1 minute' "
                "WHERE number = :n"
            ),
            {"n": number},
        )


# --- paid by hand ------------------------------------------------------------------------------


async def test_marking_paid_commits_the_reservation_and_sells_the_units(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    assert await item_row(container, placed.stock) == (5, 2)
    created = (await order_row(container, placed.number))["updated_at"]

    await pay(container, placed, "  Transferencia   directa ")

    stored = await order_row(container, placed.number)
    assert stored["status"] == "paid"
    assert stored["updated_at"] > created
    assert (await reservation_row(container, placed.reservation_id))["status"] == "committed"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (3, 0)
    [sale] = await movements(container, placed.stock, "sale")
    assert (sale["quantity"], sale["delta"], sale["on_hand_after"]) == (2, -2, 3)
    assert (sale["reason"], sale["actor_id"]) == (f"Venta: order:{placed.order_id}", "system")
    assert await movements(container, placed.made, "sale") == []  # type: ignore[arg-type]
    [event] = await history(container, placed.number)
    assert (event["from_status"], event["to_status"], event["actor"]) == (
        "pending_payment",
        "paid",
        OWNER,
    )
    assert event["note"] == "Transferencia directa"


async def test_every_move_publishes_one_status_changed_event_in_the_outbox(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)

    await pay(container, placed)

    [event] = await status_events(container)
    assert event["payload"] == {
        "order_id": str(placed.order_id),
        "number": placed.number,
        "from_status": "pending_payment",
        "to_status": "paid",
    }


async def test_a_refused_commit_rolls_back_the_whole_payment(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, units=2, quantity=2)
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1
    await lose(container, placed.stock, 2)
    assert await item_row(container, placed.stock) == (0, 0)
    sales_before = await table_count(container, "stock_movements")

    error = refused(
        await container.services.get(MarkOrderPaid).execute(placed.number, "Transferencia", OWNER)
    )

    assert error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    assert await history(container, placed.number) == []
    assert await status_events(container) == []
    assert (await reservation_row(container, placed.reservation_id))["status"] == "expired"  # type: ignore[arg-type]
    assert await table_count(container, "stock_movements") == sales_before


async def test_a_late_payment_of_an_expired_reservation_sells_when_the_units_are_still_there(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1

    await pay(container, placed)

    assert (await order_row(container, placed.number))["status"] == "paid"
    assert await item_row(container, placed.stock) == (3, 0)


async def test_two_payments_at_once_sell_the_units_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    command = container.services.get(MarkOrderPaid)

    results = await asyncio.wait_for(
        asyncio.gather(
            command.execute(placed.number, "Primera", OWNER),
            command.execute(placed.number, "Segunda", "owner-2"),
        ),
        20,
    )

    assert sorted(isinstance(r, Ok) for r in results) == [False, True]
    [loser] = [r for r in results if isinstance(r, Err)]
    assert loser.error.code == "ORDERS_TRANSITION_INVALID"
    assert len(await movements(container, placed.stock, "sale")) == 1
    assert await item_row(container, placed.stock) == (3, 0)
    assert len(await history(container, placed.number)) == 1
    assert len(await status_events(container)) == 1


# --- made to order only ------------------------------------------------------------------------


async def test_an_order_without_a_reservation_is_paid_and_cancelled_without_touching_inventory(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, made_only=True)
    assert placed.reservation_id is None
    reservations_before = await table_count(container, "reservations")
    ledger_before = await table_count(container, "stock_movements")

    await pay(container, placed)
    ok(await container.services.get(CancelPaidOrder).execute(placed.number, "Motivo", OWNER))

    stored = await order_row(container, placed.number)
    assert (stored["status"], stored["refund_status"]) == ("cancelled", "pending")
    assert await table_count(container, "reservations") == reservations_before == 0
    assert await table_count(container, "stock_movements") == ledger_before


async def test_an_unpaid_order_without_a_reservation_cancels_without_touching_inventory(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, made_only=True)

    ok(await container.services.get(CancelUnpaidOrder).execute(placed.number, "Motivo", "staff-1"))

    assert (await order_row(container, placed.number))["status"] == "cancelled"
    assert await table_count(container, "reservations") == 0


# --- forward moves -----------------------------------------------------------------------------


async def test_a_home_order_ships_with_carrier_and_tracking_then_is_delivered(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await pay(container, placed)
    advance = container.services.get(AdvanceOrder)

    refused_without_data = refused(
        await advance.execute(placed.number, "shipped", None, None, "staff-1")
    )
    ok(await advance.execute(placed.number, "shipped", "Estafeta", "123456", "staff-1"))
    ok(await advance.execute(placed.number, "delivered", None, None, "staff-2"))

    assert refused_without_data.code == "ORDERS_ORDER_DATA_INVALID"
    stored = await order_row(container, placed.number)
    assert (stored["status"], stored["shipping_carrier"], stored["tracking_number"]) == (
        "delivered",
        "Estafeta",
        "123456",
    )
    assert [
        (e["from_status"], e["to_status"], e["actor"])
        for e in await history(container, placed.number)
    ] == [
        ("pending_payment", "paid", OWNER),
        ("paid", "shipped", "staff-1"),
        ("shipped", "delivered", "staff-2"),
    ]
    assert await item_row(container, placed.stock) == (3, 0)
    assert len(await status_events(container)) == 3


async def test_a_pickup_order_goes_through_preparing_and_ready_for_pickup(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, method="pickup")
    await pay(container, placed)
    advance = container.services.get(AdvanceOrder)

    wrong = refused(await advance.execute(placed.number, "shipped", None, None, "staff-1"))
    for target in ("preparing", "ready_for_pickup", "delivered"):
        ok(await advance.execute(placed.number, target, None, None, "staff-1"))

    assert wrong.code == "ORDERS_TRANSITION_INVALID"
    assert (await order_row(container, placed.number))["status"] == "delivered"
    assert len(await history(container, placed.number)) == 4


async def test_a_move_backwards_is_refused_and_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, method="pickup")
    await pay(container, placed)
    advance = container.services.get(AdvanceOrder)
    for target in ("ready_for_pickup", "delivered"):
        ok(await advance.execute(placed.number, target, None, None, "staff-1"))

    error = refused(await advance.execute(placed.number, "preparing", None, None, "staff-1"))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert (await order_row(container, placed.number))["status"] == "delivered"
    assert len(await history(container, placed.number)) == 3


# --- cancel unpaid -----------------------------------------------------------------------------


async def test_cancelling_an_unpaid_order_releases_the_units_and_records_no_movement(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    ledger = await table_count(container, "stock_movements")

    ok(
        await container.services.get(CancelUnpaidOrder).execute(
            placed.number, "El cliente lo pidió", "staff-1"
        )
    )

    stored = await order_row(container, placed.number)
    assert (stored["status"], stored["cancel_reason"], stored["refund_status"]) == (
        "cancelled",
        "El cliente lo pidió",
        None,
    )
    assert (await reservation_row(container, placed.reservation_id))["status"] == "released"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (5, 0)
    assert await table_count(container, "stock_movements") == ledger
    [event] = await history(container, placed.number)
    assert (event["to_status"], event["actor"], event["note"]) == (
        "cancelled",
        "staff-1",
        "El cliente lo pidió",
    )


async def test_cancelling_an_order_whose_reservation_already_expired_works(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await make_due(container, placed.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1

    ok(await container.services.get(CancelUnpaidOrder).execute(placed.number, "Motivo", "staff-1"))

    assert (await order_row(container, placed.number))["status"] == "cancelled"
    assert (await reservation_row(container, placed.reservation_id))["status"] == "expired"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (5, 0)


async def test_cancelling_a_paid_order_through_the_unpaid_command_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await pay(container, placed)

    error = refused(
        await container.services.get(CancelUnpaidOrder).execute(placed.number, "Motivo", "staff-1")
    )

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert (await order_row(container, placed.number))["status"] == "paid"
    assert await item_row(container, placed.stock) == (3, 0)


# --- cancel paid and refund --------------------------------------------------------------------


async def test_cancelling_a_paid_order_returns_the_units_with_one_return_movement(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await pay(container, placed)

    ok(await container.services.get(CancelPaidOrder).execute(placed.number, "Sin producto", OWNER))

    stored = await order_row(container, placed.number)
    assert (stored["status"], stored["cancel_reason"], stored["refund_status"]) == (
        "cancelled",
        "Sin producto",
        "pending",
    )
    assert (await reservation_row(container, placed.reservation_id))["status"] == "returned"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (5, 0)
    [returned] = await movements(container, placed.stock, "return")
    assert (returned["quantity"], returned["delta"], returned["on_hand_after"]) == (2, 2, 5)
    assert (returned["reason"], returned["actor_id"]) == (
        f"Devolución: order:{placed.order_id}",
        "system",
    )
    assert await movements(container, placed.made, "return") == []  # type: ignore[arg-type]
    assert [
        (e["from_status"], e["to_status"]) for e in await history(container, placed.number)
    ] == [
        ("pending_payment", "paid"),
        ("paid", "cancelled"),
    ]


async def test_the_refund_is_marked_done_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await pay(container, placed)
    ok(await container.services.get(CancelPaidOrder).execute(placed.number, "Motivo", OWNER))
    refund = container.services.get(MarkOrderRefunded)

    ok(await refund.execute(placed.number, "Reembolso por transferencia", OWNER))
    second = refused(await refund.execute(placed.number, "Otra vez", OWNER))

    assert second.code == "ORDERS_TRANSITION_INVALID"
    stored = await order_row(container, placed.number)
    assert (stored["status"], stored["refund_status"]) == ("cancelled", "done")
    last = (await history(container, placed.number))[-1]
    assert (last["from_status"], last["to_status"], last["note"]) == (
        "cancelled",
        "cancelled",
        "Reembolso por transferencia",
    )
    assert len(await history(container, placed.number)) == 3
    assert await item_row(container, placed.stock) == (5, 0)  # the refund touches no stock
    assert len(await status_events(container)) == 3


async def test_cancel_paid_on_an_unpaid_order_changes_nothing_and_returns_no_units(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)

    error = refused(
        await container.services.get(CancelPaidOrder).execute(placed.number, "Motivo", OWNER)
    )

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    assert await item_row(container, placed.stock) == (5, 2)
    assert await movements(container, placed.stock, "return") == []


async def test_a_returned_reservation_is_idempotent_to_return_and_refuses_commit_and_release(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """Deviation 2 and the idempotence of `return_sold`, over the real SQL adapters."""
    placed = await place(container, catalog_rows)
    await pay(container, placed)
    ok(await container.services.get(CancelPaidOrder).execute(placed.number, "Motivo", OWNER))
    facade = container.services.get(StockReservations)
    reservation = placed.reservation_id
    assert reservation is not None
    ledger = await table_count(container, "stock_movements")

    again = await facade.return_sold(reservation)
    committed = await facade.commit(reservation)
    released = await facade.release(reservation)

    assert isinstance(again, Ok)
    assert refused(committed).code == "INVENTORY_RESERVATION_NOT_ACTIVE"
    assert refused(released).code == "INVENTORY_RESERVATION_COMMITTED"
    assert (await reservation_row(container, reservation))["status"] == "returned"
    assert await item_row(container, placed.stock) == (5, 0)
    assert await table_count(container, "stock_movements") == ledger


async def test_returning_a_reservation_that_was_never_sold_is_refused_by_inventory(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    facade = container.services.get(StockReservations)

    result = await facade.return_sold(placed.reservation_id)  # type: ignore[arg-type]

    assert refused(result).code == "INVENTORY_RESERVATION_NOT_COMMITTED"
    assert await item_row(container, placed.stock) == (5, 2)
    assert (await reservation_row(container, placed.reservation_id))["status"] == "active"  # type: ignore[arg-type]


# --- the overdue job ---------------------------------------------------------------------------


async def test_the_job_cancels_overdue_unpaid_orders_and_releases_their_units(
    container: Container, catalog_rows: CatalogRows
) -> None:
    active = await place(container, catalog_rows, name="Active")
    expired = await place(container, catalog_rows, name="Expired")
    await backdate_due(container, active.number)
    await backdate_due(container, expired.number)
    await make_due(container, expired.reservation_id)  # type: ignore[arg-type]
    assert await container.services.get(ExpireReservations).run_once() == 1

    count = await container.services.get(CancelOverdueOrders).run_once()

    assert count == 2
    for placed in (active, expired):
        stored = await order_row(container, placed.number)
        assert (stored["status"], stored["cancel_reason"]) == ("cancelled", "No pagado a tiempo")
        [event] = await history(container, placed.number)
        assert (event["from_status"], event["to_status"], event["actor"], event["note"]) == (
            "pending_payment",
            "cancelled",
            "system",
            "No pagado a tiempo",
        )
        assert await item_row(container, placed.stock) == (5, 0)
    assert (await reservation_row(container, active.reservation_id))["status"] == "released"  # type: ignore[arg-type]
    assert (await reservation_row(container, expired.reservation_id))["status"] == "expired"  # type: ignore[arg-type]
    events = await status_events(container)
    assert sorted(e["payload"]["number"] for e in events) == sorted([active.number, expired.number])
    assert {e["payload"]["to_status"] for e in events} == {"cancelled"}


async def test_the_job_leaves_orders_that_are_not_overdue_or_not_unpaid(
    container: Container, catalog_rows: CatalogRows
) -> None:
    waiting = await place(container, catalog_rows, name="Waiting")
    paid = await place(container, catalog_rows, name="Paid")
    await pay(container, paid)
    await backdate_due(container, paid.number)
    before = len(await status_events(container))

    count = await container.services.get(CancelOverdueOrders).run_once()

    assert count == 0
    assert (await order_row(container, waiting.number))["status"] == "pending_payment"
    assert (await order_row(container, paid.number))["status"] == "paid"
    assert (await reservation_row(container, waiting.reservation_id))["status"] == "active"  # type: ignore[arg-type]
    assert len(await status_events(container)) == before


async def test_the_job_cancels_an_overdue_order_without_a_reservation(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows, made_only=True)
    await backdate_due(container, placed.number)

    count = await container.services.get(CancelOverdueOrders).run_once()

    assert count == 1
    assert (await order_row(container, placed.number))["status"] == "cancelled"


async def test_deviation_5_the_job_skips_and_logs_an_order_inventory_refuses_and_stops_there(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """The second order's reservation was sold behind the order's back, so inventory refuses its
    release (`INVENTORY_RESERVATION_COMMITTED`). The job cancels the first order (its own
    transaction), leaves the refused one untouched and stops the run: the third, later order
    waits for the next run (review L1)."""
    normal = await place(container, catalog_rows, name="Normal")
    sold = await place(container, catalog_rows, name="Sold")
    later = await place(container, catalog_rows, name="Later")
    facade = container.services.get(StockReservations)
    assert isinstance(await facade.commit(sold.reservation_id), Ok)  # type: ignore[arg-type]
    for order in (normal, sold, later):
        await backdate_due(container, order.number)

    with capture_logs() as logs:
        count = await container.services.get(CancelOverdueOrders).run_once()

    assert count == 1
    assert (await order_row(container, normal.number))["status"] == "cancelled"
    assert await item_row(container, normal.stock) == (5, 0)
    skipped = await order_row(container, sold.number)
    assert (skipped["status"], skipped["cancel_reason"]) == ("pending_payment", None)
    assert await history(container, sold.number) == []
    assert (await reservation_row(container, sold.reservation_id))["status"] == "committed"  # type: ignore[arg-type]
    assert await item_row(container, sold.stock) == (3, 0)
    assert (await order_row(container, later.number))["status"] == "pending_payment"
    [warning] = [log for log in logs if log["event"] == "orders.overdue_release_refused"]
    assert (warning["log_level"], warning["number"], warning["code"]) == (
        "warning",
        sold.number,
        "INVENTORY_RESERVATION_COMMITTED",
    )
    assert [e["payload"]["number"] for e in await status_events(container)] == [normal.number]


async def test_two_runs_of_the_job_at_once_cancel_each_order_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = [await place(container, catalog_rows, name=f"Race {n}") for n in range(3)]
    for order in placed:
        await backdate_due(container, order.number)
    job = container.services.get(CancelOverdueOrders)

    counts = await asyncio.wait_for(asyncio.gather(job.run_once(), job.run_once()), 30)

    assert sum(counts) == 3
    for order in placed:
        assert len(await history(container, order.number)) == 1
        assert await item_row(container, order.stock) == (5, 0)


async def place_lines(
    container: Container, lines: list[tuple[UUID, int]], unit_cents: int = 250_000
) -> Placed:
    """One order through the real checkout over the given in-stock lines."""
    token = await add_to_cart(container, lines)
    total = sum(quantity for _, quantity in lines) * unit_cents + 18_000
    result = await real_place_order(container).execute(token, request("home", total))
    assert isinstance(result, Ok), result
    number = int(result.value.number.removeprefix("LFI-"))
    [order] = await rows(
        container, "SELECT id, reservation_id FROM orders.orders WHERE number = :n", n=number
    )
    return Placed(number, order["id"], lines[0][0], None, order["reservation_id"])


async def wait_until_sessions_wait_on_locks(container: Container, *, at_least: int) -> None:
    """Poll `pg_locks` until `at_least` distinct sessions wait on an ungranted lock (the test
    holds the lock, so the waiters can only be the jobs under test)."""
    query = text(
        "SELECT count(DISTINCT l.pid) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE NOT l.granted AND a.datname = current_database() AND l.pid <> pg_backend_pid()"
    )
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        async with container.database.engine.connect() as connection:
            if (await connection.execute(query)).scalar_one() >= at_least:
                return
        await asyncio.sleep(0.05)
    pytest.fail(f"fewer than {at_least} sessions ever waited on a lock: the jobs did not overlap")


async def test_review_m1_the_job_and_the_expiry_job_over_crossed_stock_do_not_deadlock(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """Orders hold crossed sets of presentations: a one-line order on the later id and a
    two-line order on both. Run in one transaction the overdue job took the items of the first
    while asking for those of the second, against inventory's expiry job doing the opposite. One
    transaction per order keeps inventory's lock order, so both jobs finish."""
    await configure_delivery(container)
    placed: list[tuple[Placed, bool]] = []
    pairs: list[tuple[UUID, UUID]] = []
    for n in range(4):
        _, ids = await catalog_rows.perfume(
            f"Crossed {n}", sizes=({"stock_available": 20}, {"stock_available": 20})
        )
        low, high = sorted(ids)
        for presentation in (low, high):
            await restock(container, presentation, 20)
        pairs.append((low, high))
        single = await place_lines(container, [(high, 1)])
        double = await place_lines(container, [(high, 1), (low, 1)])
        placed += [(single, False), (double, True)]  # the two-line one expires, the other is active
    for order, expires in placed:
        await backdate_due(container, order.number)
        if expires:
            await make_due(container, order.reservation_id)  # type: ignore[arg-type]
    overdue = container.services.get(CancelOverdueOrders)
    expiry = container.services.get(ExpireReservations)

    # Hold every stock item of the test so both jobs reach inventory and queue behind the holder:
    # the interleaving is then proven (two sessions waiting on a lock at once), not hoped for.
    item_ids = [presentation for pair in pairs for presentation in pair]
    async with container.database.engine.connect() as holder:
        await holder.execute(
            text(
                "SELECT 1 FROM inventory.stock_items WHERE presentation_id = ANY(:ids) "
                "ORDER BY presentation_id FOR UPDATE"
            ),
            {"ids": item_ids},
        )
        jobs = asyncio.gather(overdue.run_once(), expiry.run_once())
        await wait_until_sessions_wait_on_locks(container, at_least=2)
        await holder.rollback()  # release the items: both jobs now race for them
    await asyncio.wait_for(jobs, 30)
    await asyncio.wait_for(overdue.run_once(), 30)  # whatever the expiry job held at the time

    for order, _ in placed:
        assert (await order_row(container, order.number))["status"] == "cancelled"
        assert (await reservation_row(container, order.reservation_id))["status"] in {  # type: ignore[arg-type]
            "released",
            "expired",
        }
    for low, high in pairs:
        assert await item_row(container, low) == (20, 0)
        assert await item_row(container, high) == (20, 0)
