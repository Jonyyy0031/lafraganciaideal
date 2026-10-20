"""Plan 005 step 3 on PostgreSQL (`fragancia_test`): `SqlCustomerOrders` (the view by digest, its
lines, timeline, `cancelled_by` from the cancelling move's actor, the refund row left out, the exact
key set, `match` on number and normalized email) and `SqlLookupThrottle` (atomic counting under
concurrency, the window reset, independent keys, joining the active transaction)."""

import asyncio
from datetime import timedelta

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.orders.domain.order import Order, OrderStatus
from fragancia_api.modules.orders.infrastructure.sql_customer_orders import SqlCustomerOrders
from fragancia_api.modules.orders.infrastructure.sql_lookup_throttle import SqlLookupThrottle
from fragancia_api.modules.orders.infrastructure.sql_order_repository import SqlOrderRepository
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
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
from tests.unit.orders.test_customer_order import (
    ADDRESS_KEYS,
    FORBIDDEN,
    LINE_KEYS,
    STEP_KEYS,
    VIEW_KEYS,
)

pytestmark = pytest.mark.integration

T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)
S = OrderStatus
WINDOW = timedelta(minutes=15)


def queries(container: Container) -> SqlCustomerOrders:
    return SqlCustomerOrders(container.database)


async def load(container: Container, number: int) -> Order:
    repo = SqlOrderRepository(container.database)
    found = await in_transaction(container, lambda: repo.get_by_number_for_update(number))
    assert found is not None
    return found


async def save(container: Container, order: Order) -> None:
    repo = SqlOrderRepository(container.database)
    await in_transaction(container, lambda: repo.save(order))


async def view_of(container: Container, digest: str):  # type: ignore[no-untyped-def]
    found = await queries(container).by_access_digest(digest)
    assert found is not None
    return found


# --- by_access_digest --------------------------------------------------------------------------


async def test_an_unknown_digest_finds_nothing(container: Container) -> None:
    await store_order(container, make_order(digest="a" * 64))

    assert await queries(container).by_access_digest("b" * 64) is None


async def test_a_new_home_order_is_shown_with_its_lines_totals_and_address(
    container: Container,
) -> None:
    await store_order(container, make_order(digest="a" * 64, invoice=True))

    view = await view_of(container, "a" * 64)

    assert (view.number, view.status, view.created_at) == ("LFI-1001", "pending_payment", T0)
    assert view.payment_due_at == T0 + timedelta(minutes=30)
    assert [(line.label, line.quantity) for line in view.lines] == [
        ("Brand Perfume 1 EDT 100 ml", 2),
        ("Brand Perfume 2 EDT 100 ml", 1),
    ]
    first = view.lines[0]
    assert (first.ml, first.unit_price_cents, first.line_total_cents) == (100, 250_000, 500_000)
    assert first.availability == "in_stock"
    assert (view.subtotal_cents, view.delivery_cents, view.total_cents) == (
        750_000,
        18_000,
        768_000,
    )
    assert view.delivery_method == "home"
    assert view.address is not None
    assert (view.address.street, view.address.interior_number, view.address.state) == (
        "Av. Reforma",
        "4B",
        "CMX",
    )
    assert view.invoice is not None
    assert (view.invoice.rfc, view.invoice.postal_code) == ("XAXX010101000", "01000")
    assert (view.pickup_address, view.pickup_hours) == (None, None)
    assert view.can_cancel is True
    assert [(s.status, s.at) for s in view.timeline] == [("pending_payment", T0)]
    assert (view.cancelled_by, view.refund_status) == (None, None)


async def test_a_pickup_order_shows_the_pickup_data_and_no_address_or_invoice(
    container: Container,
) -> None:
    await store_order(container, make_order(digest="a" * 64, pickup=True))

    view = await view_of(container, "a" * 64)

    assert view.delivery_method == "pickup"
    assert (view.address, view.invoice) == (None, None)
    assert (view.pickup_address, view.pickup_hours) == ("Calle 1 #2, Cancun", "Lun-Vie 10-18")


async def test_the_lines_come_in_their_stored_position(container: Container) -> None:
    lines = (order_line(3), order_line(1), order_line(2))
    await store_order(container, make_order(digest="a" * 64, lines=lines))

    view = await view_of(container, "a" * 64)

    assert [line.label for line in view.lines] == [
        "Brand Perfume 3 EDT 100 ml",
        "Brand Perfume 1 EDT 100 ml",
        "Brand Perfume 2 EDT 100 ml",
    ]


async def test_a_made_to_order_line_carries_its_lead_times(container: Container) -> None:
    lines = (order_line(1, availability="made_to_order", max_days=10),)
    await store_order(container, make_order(digest="a" * 64, lines=lines))

    view = await view_of(container, "a" * 64)

    assert view.lines[0].availability == "made_to_order"
    assert (view.lines[0].lead_time_min_days, view.lines[0].lead_time_max_days) == (8, 10)
    assert view.lead_time_max_days == 10


async def test_the_timeline_follows_the_history_oldest_first_and_carries_the_shipping_data(
    container: Container,
) -> None:
    await store_order(container, make_order(digest="a" * 64))
    order = await load(container, 1001)
    unwrap(order.mark_paid("Transferencia directa", "admin-1", T1))
    unwrap(order.ship("Estafeta", "123456", "admin-2", T2))
    await save(container, order)

    view = await view_of(container, "a" * 64)

    assert [(s.status, s.at) for s in view.timeline] == [
        ("pending_payment", T0),
        ("paid", T1),
        ("shipped", T2),
    ]
    assert (view.status, view.can_cancel, view.cancelled_by) == ("shipped", False, None)
    assert (view.shipping_carrier, view.tracking_number) == ("Estafeta", "123456")


@pytest.mark.parametrize(
    ("actor", "expected"), [("customer", "customer"), ("system", "system"), ("owner-1", "shop")]
)
async def test_cancelled_by_comes_from_the_actor_of_the_cancelling_move(
    container: Container, actor: str, expected: str
) -> None:
    await store_order(container, make_order(digest="a" * 64))
    order = await load(container, 1001)
    unwrap(order.cancel_unpaid("un motivo privado", actor, T1))
    await save(container, order)

    view = await view_of(container, "a" * 64)

    assert (view.status, view.cancelled_by, view.can_cancel) == ("cancelled", expected, False)
    assert [s.status for s in view.timeline] == ["pending_payment", "cancelled"]
    assert "un motivo privado" not in view.model_dump_json()
    assert actor not in view.model_dump_json() or actor in ("customer", "system")


async def test_the_refund_row_is_not_a_step_and_refund_status_says_it(
    container: Container,
) -> None:
    await store_order(container, make_order(digest="a" * 64))
    order = await load(container, 1001)
    unwrap(order.mark_paid("x", "admin-1", T1))
    unwrap(order.cancel_paid("Sin producto", "admin-1", T2))
    unwrap(order.mark_refunded("Reembolsado", "admin-1", T3))
    await save(container, order)
    assert len(await rows(container, "SELECT 1 FROM orders.order_events")) == 3

    view = await view_of(container, "a" * 64)

    assert [(s.status, s.at) for s in view.timeline] == [
        ("pending_payment", T0),
        ("paid", T1),
        ("cancelled", T2),
    ]
    assert (view.refund_status, view.cancelled_by) == ("done", "shop")


async def test_the_view_has_exactly_the_customer_keys_and_no_internal_value(
    container: Container,
) -> None:
    order = make_order(digest="a" * 64, invoice=True, reservation_id=None)
    await store_order(container, order)
    stored = await load(container, 1001)
    unwrap(stored.mark_paid("Transferencia directa", "owner-secret", T1))
    await save(container, stored)
    await in_transaction(
        container,
        lambda: SqlOrderRepository(container.database).add_note(
            1001, "owner-secret", "nota interna secreta", T1
        ),
    )

    view = await view_of(container, "a" * 64)

    dumped = view.model_dump(mode="json")
    assert set(dumped) == VIEW_KEYS
    assert set(dumped["lines"][0]) == LINE_KEYS
    assert set(dumped["address"]) == ADDRESS_KEYS
    assert all(set(step) == STEP_KEYS for step in dumped["timeline"])
    assert FORBIDDEN.isdisjoint(dumped)
    text = view.model_dump_json()
    for secret in ("owner-secret", "nota interna secreta", "Transferencia directa", "a" * 64):
        assert secret not in text, secret
    assert "ana@example.com" not in text


async def test_each_digest_opens_only_its_own_order(container: Container) -> None:
    await store_order(container, make_order(number=1001, digest="a" * 64))
    await store_order(container, make_order(number=1002, digest="b" * 64, cart_digest="d" * 64))

    first = await view_of(container, "a" * 64)
    second = await view_of(container, "b" * 64)

    assert (first.number, second.number) == ("LFI-1001", "LFI-1002")


# --- match -------------------------------------------------------------------------------------


async def test_match_finds_the_order_by_number_and_its_stored_email(container: Container) -> None:
    order = make_order(number=1001)
    await store_order(container, order)

    found = await queries(container).match(1001, "ana@example.com")

    assert found == (order.id, 1001)


async def test_match_refuses_a_wrong_email_an_unknown_number_and_a_swapped_pair(
    container: Container,
) -> None:
    await store_order(container, make_order(number=1001))
    await store_order(container, make_order(number=1002, cart_digest="d" * 64))
    sql = queries(container)

    assert await sql.match(1001, "otro@example.com") is None
    assert await sql.match(9999, "ana@example.com") is None
    assert await sql.match(1001, "ANA@EXAMPLE.COM") is None  # the caller normalizes first
    assert await sql.match(1001, "") is None


# --- SqlLookupThrottle -------------------------------------------------------------------------


def throttle(container: Container) -> SqlLookupThrottle:
    return SqlLookupThrottle(container.database)


async def hit(container: Container, key: str, at, window: timedelta = WINDOW) -> int:  # type: ignore[no-untyped-def]
    t = throttle(container)
    return await in_transaction(container, lambda: t.hit(key, at, window))


async def test_the_first_hit_counts_one_and_stores_the_window_start(container: Container) -> None:
    assert await hit(container, "lookup-ip:1.1.1.1", T0) == 1

    stored = await row(container, "SELECT * FROM orders.lookup_throttle")
    assert (stored["key"], stored["attempts"], stored["window_started_at"]) == (
        "lookup-ip:1.1.1.1",
        1,
        T0,
    )


async def test_hits_inside_the_window_count_up_and_keep_the_window_start(
    container: Container,
) -> None:
    results = [await hit(container, "k", T0 + timedelta(minutes=m)) for m in (0, 1, 7, 14)]

    assert results == [1, 2, 3, 4]
    stored = await row(container, "SELECT attempts, window_started_at FROM orders.lookup_throttle")
    assert (stored["attempts"], stored["window_started_at"]) == (4, T0)


async def test_a_hit_exactly_one_window_later_starts_a_new_window(container: Container) -> None:
    await hit(container, "k", T0)
    await hit(container, "k", T0 + timedelta(minutes=5))

    assert await hit(container, "k", T0 + WINDOW) == 1

    stored = await row(container, "SELECT attempts, window_started_at FROM orders.lookup_throttle")
    assert (stored["attempts"], stored["window_started_at"]) == (1, T0 + WINDOW)


async def test_the_last_instant_before_the_window_ends_still_counts_in_it(
    container: Container,
) -> None:
    await hit(container, "k", T0)

    assert await hit(container, "k", T0 + WINDOW - timedelta(seconds=1)) == 2


async def test_keys_count_independently(container: Container) -> None:
    await hit(container, "lookup-ip:1.1.1.1", T0)
    await hit(container, "lookup-ip:1.1.1.1", T0)

    assert await hit(container, "lookup-number:1001", T0) == 1
    assert await hit(container, "lookup-ip:2.2.2.2", T0) == 1
    assert len(await rows(container, "SELECT 1 FROM orders.lookup_throttle")) == 3


async def test_concurrent_hits_on_one_key_each_get_their_own_count(container: Container) -> None:
    await hit(container, "k", T0)  # the row exists, so every hit takes the conflict path

    counts = await asyncio.wait_for(
        asyncio.gather(*(hit(container, "k", T0) for _ in range(10))), TIMEOUT
    )

    assert sorted(counts) == list(range(2, 12))
    assert (await row(container, "SELECT attempts FROM orders.lookup_throttle"))["attempts"] == 11


async def test_concurrent_first_hits_on_a_new_key_do_not_lose_an_attempt(
    container: Container,
) -> None:
    counts = await asyncio.wait_for(
        asyncio.gather(*(hit(container, "fresh", T0) for _ in range(8))), TIMEOUT
    )

    assert sorted(counts) == list(range(1, 9))
    assert (await row(container, "SELECT attempts FROM orders.lookup_throttle"))["attempts"] == 8


async def test_a_hit_joins_the_active_transaction_and_a_rollback_undoes_it(
    container: Container,
) -> None:
    t = throttle(container)

    async def refused() -> Result[None, DomainError]:
        await t.hit("k", T0, WINDOW)
        return Err(DomainError(code="X", message="refused"))

    result = await SqlTransactionRunner(container.database).run(refused)

    assert isinstance(result, Err)
    assert await rows(container, "SELECT 1 FROM orders.lookup_throttle") == []


async def test_a_committed_hit_survives_a_later_refusal_outside_its_transaction(
    container: Container,
) -> None:
    t = throttle(container)

    async def count() -> Result[int, DomainError]:
        return Ok(await t.hit("k", T0, WINDOW))

    committed = await SqlTransactionRunner(container.database).run(count)

    assert isinstance(committed, Ok)
    assert (await row(container, "SELECT attempts FROM orders.lookup_throttle"))["attempts"] == 1
