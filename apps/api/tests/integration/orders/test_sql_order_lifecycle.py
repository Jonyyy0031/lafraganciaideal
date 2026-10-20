"""Plan 004 step 5 on PostgreSQL (`fragancia_test`): `SqlOrderRepository` (`save` with its history,
`get_by_number_for_update`, `claim_overdue` with `SKIP LOCKED`, `add_note`) and `SqlOrderQueries`
(status and search filters, ordering, paging, the detail)."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.orders.application.commands.lifecycle import AddOrderNote
from fragancia_api.modules.orders.domain.customer import CustomerEmail, CustomerName
from fragancia_api.modules.orders.domain.order import Order, OrderStatus, RefundStatus
from fragancia_api.modules.orders.infrastructure.sql_order_queries import SqlOrderQueries
from fragancia_api.modules.orders.infrastructure.sql_order_repository import SqlOrderRepository
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import (
    T0,
    TIMEOUT,
    make_order,
    order_line,
    row,
    rows,
    store_order,
    unwrap,
)

pytestmark = pytest.mark.integration

T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)
S = OrderStatus


def repo(container: Container) -> SqlOrderRepository:
    return SqlOrderRepository(container.database)


async def load(container: Container, number: int) -> Order | None:
    r = repo(container)
    return await in_transaction(container, lambda: r.get_by_number_for_update(number))


async def load_order(container: Container, number: int) -> Order:
    found = await load(container, number)
    assert found is not None
    return found


async def save(container: Container, order: Order) -> None:
    r = repo(container)
    await in_transaction(container, lambda: r.save(order))


# --- get_by_number_for_update and save ---------------------------------------------------------


async def test_an_unknown_number_loads_nothing(container: Container) -> None:
    assert await load(container, 4242) is None


async def test_a_loaded_order_carries_its_lines_and_the_lifecycle_fields(
    container: Container,
) -> None:
    placed = make_order(number=1001, reservation_id=UUID(int=9))
    await store_order(container, placed)

    loaded = await load_order(container, 1001)

    assert (loaded.id, loaded.number, loaded.status) == (placed.id, 1001, S.PENDING_PAYMENT)
    assert [line.quantity for line in loaded.lines] == [2, 1]
    assert loaded.reservation_id == UUID(int=9)
    assert (loaded.cancel_reason, loaded.refund_status) == (None, None)
    assert (loaded.shipping_carrier, loaded.tracking_number) == (None, None)
    assert loaded.new_history == []


async def test_save_updates_the_status_and_inserts_the_history_row(container: Container) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("  Transferencia   directa ", "admin-1", T1))

    await save(container, order)

    stored = await row(container, "SELECT status, updated_at FROM orders.orders")
    assert (stored["status"], stored["updated_at"]) == ("paid", T1)
    [event] = await rows(container, "SELECT * FROM orders.order_events")
    assert (event["order_id"], event["at"]) == (order.id, T1)
    assert (event["from_status"], event["to_status"], event["actor"]) == (
        "pending_payment",
        "paid",
        "admin-1",
    )
    assert (event["note"], event["carrier"], event["tracking_number"]) == (
        "Transferencia directa",
        None,
        None,
    )
    assert order.new_history == []


async def test_saving_again_does_not_insert_the_history_twice(container: Container) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("x", "admin-1", T1))
    await save(container, order)

    await save(container, order)

    assert len(await rows(container, "SELECT 1 FROM orders.order_events")) == 1


async def test_shipping_stores_the_carrier_and_tracking_on_the_order_and_in_the_history(
    container: Container,
) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("x", "admin-1", T1))
    unwrap(order.ship("Estafeta", "123456", "admin-2", T2))

    await save(container, order)

    stored = await row(
        container, "SELECT status, shipping_carrier, tracking_number FROM orders.orders"
    )
    assert stored == {
        "status": "shipped",
        "shipping_carrier": "Estafeta",
        "tracking_number": "123456",
    }
    events = await rows(container, "SELECT * FROM orders.order_events ORDER BY at")
    assert [(e["from_status"], e["to_status"], e["actor"]) for e in events] == [
        ("pending_payment", "paid", "admin-1"),
        ("paid", "shipped", "admin-2"),
    ]
    assert (events[1]["carrier"], events[1]["tracking_number"]) == ("Estafeta", "123456")


async def test_a_cancelled_paid_order_stores_the_reason_and_the_pending_refund(
    container: Container,
) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("x", "admin-1", T1))
    unwrap(order.cancel_paid("Sin stock real", "owner-1", T2))
    await save(container, order)

    stored = await row(container, "SELECT status, cancel_reason, refund_status FROM orders.orders")
    assert stored == {
        "status": "cancelled",
        "cancel_reason": "Sin stock real",
        "refund_status": "pending",
    }
    reloaded = await load_order(container, 1001)
    assert (reloaded.status, reloaded.cancel_reason, reloaded.refund_status) == (
        S.CANCELLED,
        "Sin stock real",
        RefundStatus.PENDING,
    )

    unwrap(reloaded.mark_refunded("Reembolso", "owner-1", T3))
    await save(container, reloaded)

    assert (await row(container, "SELECT refund_status FROM orders.orders"))[
        "refund_status"
    ] == "done"
    events = await rows(container, "SELECT * FROM orders.order_events ORDER BY at")
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        ("pending_payment", "paid"),
        ("paid", "cancelled"),
        ("cancelled", "cancelled"),
    ]
    assert events[2]["note"] == "Reembolso"


async def test_a_history_row_with_every_field_fits_the_column_sizes(container: Container) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("n" * 300, "a" * 36, T1))
    unwrap(order.ship("c" * 60, "t" * 60, "a" * 36, T2))

    await save(container, order)

    events = await rows(container, "SELECT note, carrier, tracking_number FROM orders.order_events")
    assert len(events) == 2


async def test_a_rolled_back_save_leaves_the_order_and_the_history_untouched(
    container: Container,
) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("x", "admin-1", T1))
    r = repo(container)

    async def refused() -> Result[None, DomainError]:
        await r.save(order)
        return Err(DomainError(code="TEST_REFUSED", message="refused"))

    await SqlTransactionRunner(container.database).run(refused)

    assert (await row(container, "SELECT status FROM orders.orders"))["status"] == "pending_payment"
    assert await rows(container, "SELECT 1 FROM orders.order_events") == []


# --- claim_overdue -----------------------------------------------------------------------------


async def store_overdue_world(container: Container) -> None:
    """With `now = T1`: 1001 due T0+30m and 1003 due T0-30m are overdue and unpaid; 1002 is due
    exactly `now`; 1004 is paid, 1005 cancelled, both overdue; 1006 is due later."""
    await store_order(container, make_order(number=1001, at=T0))
    await store_order(container, make_order(number=1002, at=T0 + timedelta(minutes=30)))
    await store_order(container, make_order(number=1003, at=T0 - timedelta(hours=1)))
    paid = make_order(number=1004, at=T0 - timedelta(hours=1))
    paid.status = S.PAID
    await store_order(container, paid)
    cancelled = make_order(number=1005, at=T0 - timedelta(hours=1))
    cancelled.status = S.CANCELLED
    await store_order(container, cancelled)
    await store_order(container, make_order(number=1006, at=T1))


async def claim(container: Container, limit: int = 100) -> list[int]:
    r = repo(container)
    claimed = await in_transaction(container, lambda: r.claim_overdue(T1, limit))
    return [order.number for order in claimed]


async def test_claim_takes_only_unpaid_orders_due_before_now_oldest_due_first(
    container: Container,
) -> None:
    await store_overdue_world(container)

    assert await claim(container) == [1003, 1001]


async def test_an_order_due_exactly_now_is_not_overdue(container: Container) -> None:
    await store_order(container, make_order(number=1001, at=T0 + timedelta(minutes=30)))

    assert await claim(container) == []


async def test_claim_respects_the_limit_and_returns_the_oldest_first(container: Container) -> None:
    await store_overdue_world(container)

    assert await claim(container, 1) == [1003]


async def test_a_claimed_order_is_complete_with_its_lines(container: Container) -> None:
    await store_order(container, make_order(number=1001, at=T0, reservation_id=UUID(int=5)))
    r = repo(container)

    [claimed] = await in_transaction(container, lambda: r.claim_overdue(T1, 10))

    assert claimed.status is S.PENDING_PAYMENT
    assert claimed.reservation_id == UUID(int=5)
    assert [line.quantity for line in claimed.lines] == [2, 1]


async def test_claim_with_nothing_overdue_is_empty(container: Container) -> None:
    assert await claim(container) == []


async def test_a_second_claim_skips_the_rows_the_first_one_holds_without_waiting(
    container: Container,
) -> None:
    await store_overdue_world(container)
    r = repo(container)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def first_work() -> list[int]:
        claimed = await r.claim_overdue(T1, 100)
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)
        return [order.number for order in claimed]

    first = asyncio.create_task(in_transaction(container, first_work))
    await asyncio.wait_for(holding.wait(), TIMEOUT)
    try:
        second = await asyncio.wait_for(claim(container), TIMEOUT)
    finally:
        release.set()
    held = await asyncio.wait_for(first, TIMEOUT)

    assert held == [1003, 1001]
    assert second == [], "SKIP LOCKED: the second run takes nothing and does not wait"


async def test_claim_skips_an_order_another_transaction_is_moving(container: Container) -> None:
    await store_overdue_world(container)
    r = repo(container)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def moving() -> None:
        await r.get_by_number_for_update(1001)
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)

    mover = asyncio.create_task(in_transaction(container, moving))
    await asyncio.wait_for(holding.wait(), TIMEOUT)
    try:
        claimed = await asyncio.wait_for(claim(container), TIMEOUT)
    finally:
        release.set()
    await asyncio.wait_for(mover, TIMEOUT)

    assert claimed == [1003]


# --- add_note ----------------------------------------------------------------------------------


async def test_add_note_stores_the_note_and_reports_true(container: Container) -> None:
    order = make_order(number=1001)
    await store_order(container, order)
    r = repo(container)

    stored = await in_transaction(container, lambda: r.add_note(1001, "admin-1", "Hola", T1))

    assert stored is True
    [note] = await rows(container, "SELECT * FROM orders.order_notes")
    assert (note["order_id"], note["at"], note["actor"], note["text"]) == (
        order.id,
        T1,
        "admin-1",
        "Hola",
    )


async def test_add_note_for_an_unknown_number_stores_nothing_and_reports_false(
    container: Container,
) -> None:
    r = repo(container)

    stored = await in_transaction(container, lambda: r.add_note(4242, "admin-1", "Hola", T1))

    assert stored is False
    assert await rows(container, "SELECT 1 FROM orders.order_notes") == []


async def test_a_note_does_not_touch_the_order_row(container: Container) -> None:
    await store_order(container, make_order(number=1001))
    before = await row(container, "SELECT status, updated_at FROM orders.orders")
    r = repo(container)

    await in_transaction(container, lambda: r.add_note(1001, "admin-1", "Hola", T3))

    assert await row(container, "SELECT status, updated_at FROM orders.orders") == before


async def test_a_note_lands_while_a_move_holds_the_order_row(
    container: Container,
) -> None:
    """Review L2: the order row is locked `FOR NO KEY UPDATE`, which does not conflict with the
    `FOR KEY SHARE` the note's foreign key takes, so the note commits while the move is still in
    flight."""
    await store_order(container, make_order(number=1001))
    r = repo(container)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def moving() -> None:
        await r.get_by_number_for_update(1001)
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)

    mover = asyncio.create_task(in_transaction(container, moving))
    await asyncio.wait_for(holding.wait(), TIMEOUT)
    add_note = container.services.get(AddOrderNote)
    try:
        result = await asyncio.wait_for(add_note.execute(1001, "Hola", "admin-1"), 3)
        landed = len(await rows(container, "SELECT 1 FROM orders.order_notes"))
    finally:
        release.set()
        await asyncio.wait_for(mover, TIMEOUT)

    assert isinstance(result, Ok)
    assert landed == 1, "the note was committed while the move was still in flight"


# --- SqlOrderQueries: page ---------------------------------------------------------------------


def queries(container: Container) -> SqlOrderQueries:
    return SqlOrderQueries(container.database)


async def store_listing(container: Container) -> None:
    """1001 Ana pending, 1002 Beto paid, 1003 Carla delivered, 1004 Dani cancelled with a pending
    refund, 1005 Eva pending (pickup)."""
    people = [
        (1001, "Ana López", "ana@example.com", S.PENDING_PAYMENT),
        (1002, "Beto Ruiz", "beto@correo.mx", S.PAID),
        (1003, "Carla Ruiz", "carla@example.com", S.DELIVERED),
        (1004, "Dani Soto", "dani@correo.mx", S.CANCELLED),
        (1005, "Eva Soto", "eva@example.com", S.PENDING_PAYMENT),
    ]
    for number, name, email, status in people:
        order = make_order(number=number, pickup=number == 1005, at=T0 + timedelta(minutes=number))
        order.name = CustomerName(name)
        order.email = CustomerEmail(email)
        order.status = status
        await store_order(container, order)
        if status is S.CANCELLED:
            # `add` writes a new order; the refund flag is set the way `save` would.
            order.refund_status = RefundStatus.PENDING
            await save(container, order)


async def numbers(container: Container, **filters: object) -> list[str]:
    page = await queries(container).page(
        filters.get("status"),  # type: ignore[arg-type]
        filters.get("q"),  # type: ignore[arg-type]
        int(filters.get("page", 1)),  # type: ignore[call-overload]
        int(filters.get("size", 20)),  # type: ignore[call-overload]
    )
    return [item.number for item in page.items]


async def test_the_list_is_newest_number_first_with_every_summary_field(
    container: Container,
) -> None:
    await store_listing(container)

    page = await queries(container).page(None, None, 1, 20)

    assert [item.number for item in page.items] == [
        f"LFI-{n}" for n in (1005, 1004, 1003, 1002, 1001)
    ]
    assert (page.total, page.page, page.size) == (5, 1, 20)
    first = page.items[0]
    assert (first.customer_name, first.customer_email, first.status) == (
        "Eva Soto",
        "eva@example.com",
        "pending_payment",
    )
    assert (first.delivery_method, first.total_cents, first.refund_status) == (
        "pickup",
        750_000,
        None,
    )
    assert first.created_at == T0 + timedelta(minutes=1005)
    cancelled = page.items[1]
    assert (cancelled.status, cancelled.refund_status, cancelled.delivery_method) == (
        "cancelled",
        "pending",
        "home",
    )


async def test_the_list_filters_by_status(container: Container) -> None:
    await store_listing(container)

    assert await numbers(container, status="pending_payment") == ["LFI-1005", "LFI-1001"]
    assert await numbers(container, status="paid") == ["LFI-1002"]
    assert await numbers(container, status="shipped") == []


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("LFI-1001", ["LFI-1001"]),
        ("lfi-1002", ["LFI-1002"]),
        ("1003", ["LFI-1003"]),
        ("ana", ["LFI-1001"]),
        ("ANA LÓ", ["LFI-1001"]),
        ("ruiz", ["LFI-1003", "LFI-1002"]),
        ("RUIZ", ["LFI-1003", "LFI-1002"]),
        ("example.com", ["LFI-1005", "LFI-1003", "LFI-1001"]),
        ("@correo", ["LFI-1004", "LFI-1002"]),
        ("soto", ["LFI-1005", "LFI-1004"]),
        ("9999", []),
        ("LFI-9999", []),
    ],
)
async def test_the_search_matches_number_email_or_name_ignoring_case(
    container: Container, q: str, expected: list[str]
) -> None:
    await store_listing(container)

    assert await numbers(container, q=q) == expected


@pytest.mark.parametrize("q", ["%", "_", "\\", "a%a", "%%"])
async def test_the_search_treats_like_wildcards_as_plain_text(container: Container, q: str) -> None:
    await store_listing(container)

    assert await numbers(container, q=q) == []


async def test_status_and_search_combine(container: Container) -> None:
    await store_listing(container)

    assert await numbers(container, status="pending_payment", q="soto") == ["LFI-1005"]
    assert await numbers(container, status="paid", q="soto") == []


async def test_paging_slices_the_list_and_total_counts_every_match(container: Container) -> None:
    await store_listing(container)
    q = queries(container)

    first = await q.page(None, None, 1, 2)
    second = await q.page(None, None, 2, 2)
    third = await q.page(None, None, 3, 2)
    beyond = await q.page(None, None, 4, 2)

    assert [i.number for i in first.items] == ["LFI-1005", "LFI-1004"]
    assert [i.number for i in second.items] == ["LFI-1003", "LFI-1002"]
    assert [i.number for i in third.items] == ["LFI-1001"]
    assert beyond.items == []
    assert {p.total for p in (first, second, third, beyond)} == {5}
    assert (second.page, second.size) == (2, 2)


async def test_the_total_follows_the_filter_not_the_page(container: Container) -> None:
    await store_listing(container)

    page = await queries(container).page("pending_payment", None, 1, 1)

    assert (len(page.items), page.total) == (1, 2)


async def test_an_empty_list_is_an_empty_page(container: Container) -> None:
    page = await queries(container).page(None, None, 1, 20)

    assert (page.items, page.total) == ([], 0)


# --- SqlOrderQueries: detail -------------------------------------------------------------------


async def test_an_unknown_number_has_no_detail(container: Container) -> None:
    assert await queries(container).detail(4242) is None


async def test_the_detail_has_the_header_lines_address_invoice_and_reservation(
    container: Container,
) -> None:
    reservation = new_id()
    order = make_order(number=1001, invoice=True, reservation_id=reservation)
    await store_order(container, order)

    detail = await queries(container).detail(1001)

    assert detail is not None
    assert (detail.number, detail.status) == ("LFI-1001", "pending_payment")
    assert (detail.created_at, detail.updated_at) == (T0, T0)
    assert detail.payment_due_at == T0 + timedelta(minutes=30)
    assert (detail.customer_name, detail.customer_email, detail.customer_phone) == (
        "Ana López",
        "ana@example.com",
        "9981234567",
    )
    assert (detail.delivery_method, detail.delivery_cents) == ("home", 18_000)
    assert detail.address is not None
    assert (detail.address.street, detail.address.interior_number, detail.address.state) == (
        "Av. Reforma",
        "4B",
        "CMX",
    )
    assert (detail.pickup_address, detail.pickup_hours) == (None, None)
    assert detail.invoice is not None
    assert (detail.invoice.rfc, detail.invoice.cfdi_use) == ("XAXX010101000", "G03")
    assert [(line.presentation_id, line.quantity) for line in detail.lines] == [
        (UUID(int=1), 2),
        (UUID(int=2), 1),
    ]
    assert (detail.subtotal_cents, detail.total_cents) == (750_000, 768_000)
    assert detail.reservation_id == reservation
    assert (detail.cancel_reason, detail.refund_status) == (None, None)
    assert (detail.shipping_carrier, detail.tracking_number) == (None, None)
    assert (detail.history, detail.notes) == ([], [])


async def test_a_pickup_detail_has_the_pickup_point_and_no_address(container: Container) -> None:
    await store_order(
        container,
        make_order(
            number=1001,
            pickup=True,
            lines=(order_line(1, 1, availability="made_to_order", max_days=5),),
        ),
    )

    detail = await queries(container).detail(1001)

    assert detail is not None
    assert detail.address is None
    assert (detail.pickup_address, detail.pickup_hours) == ("Calle 1 #2, Cancun", "Lun-Vie 10-18")
    assert (detail.reservation_id, detail.invoice, detail.lead_time_max_days) == (None, None, 5)


async def test_the_history_is_oldest_first_and_the_notes_newest_first(
    container: Container,
) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("Transferencia", "admin-1", T1))
    unwrap(order.ship("Estafeta", "123456", "admin-2", T2))
    await save(container, order)
    r = repo(container)
    await in_transaction(container, lambda: r.add_note(1001, "admin-1", "primera", T1))
    await in_transaction(container, lambda: r.add_note(1001, "admin-2", "segunda", T3))
    await in_transaction(container, lambda: r.add_note(1001, "admin-1", "intermedia", T2))

    detail = await queries(container).detail(1001)

    assert detail is not None
    assert [(e.from_status, e.to_status, e.actor, e.at) for e in detail.history] == [
        ("pending_payment", "paid", "admin-1", T1),
        ("paid", "shipped", "admin-2", T2),
    ]
    assert (detail.history[0].note, detail.history[0].carrier) == ("Transferencia", None)
    assert (detail.history[1].carrier, detail.history[1].tracking_number) == (
        "Estafeta",
        "123456",
    )
    assert [(n.text, n.actor) for n in detail.notes] == [
        ("segunda", "admin-2"),
        ("intermedia", "admin-1"),
        ("primera", "admin-1"),
    ]
    assert detail.status == "shipped"
    assert (detail.shipping_carrier, detail.tracking_number) == ("Estafeta", "123456")
    assert detail.updated_at == T2


async def test_a_cancelled_paid_detail_shows_the_reason_and_the_refund(
    container: Container,
) -> None:
    await store_order(container, make_order(number=1001))
    order = await load_order(container, 1001)
    unwrap(order.mark_paid("x", "admin-1", T1))
    unwrap(order.cancel_paid("Sin stock real", "owner-1", T2))
    await save(container, order)

    detail = await queries(container).detail(1001)

    assert detail is not None
    assert (detail.status, detail.cancel_reason, detail.refund_status) == (
        "cancelled",
        "Sin stock real",
        "pending",
    )
    assert detail.history[-1].note == "Sin stock real"
