"""Plan 004 step 4, application: every lifecycle command over the in-memory adapters (which
inventory call it makes, none without a reservation, a refusal passing through with the order
untouched), the note command, and the overdue job (its claim, reason, actor and deviation 5: a
refused order is skipped and logged). The real rollback is in the integration tests; here the fakes
prove the ORDER of the steps: inventory refuses before the order is saved."""

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.orders.application.commands.lifecycle import (
    OVERDUE_BATCH,
    AddOrderNote,
    AdvanceOrder,
    CancelOverdueOrders,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderPaid,
    MarkOrderRefunded,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod
from fragancia_api.modules.orders.domain.order import (
    Order,
    OrderStatus,
    OrderStatusChanged,
    RefundStatus,
)
from fragancia_api.modules.orders.infrastructure.in_memory import (
    InMemoryOrders,
    InMemoryStockHolds,
)
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW
from tests.unit.orders.test_order_lifecycle_domain import make_order

ADMIN = "admin-1"
S = OrderStatus


def refusal(code: str = "INVENTORY_STOCK_UNAVAILABLE") -> DomainError:
    """A stand-in for an inventory refusal; only its `code` matters to the orders module."""
    return ConflictError(code=code, message="refused")


@dataclass
class World:
    orders: InMemoryOrders = field(default_factory=InMemoryOrders)
    stocks: InMemoryStockHolds = field(default_factory=InMemoryStockHolds)

    def __post_init__(self) -> None:
        self.events = RecordingEventPublisher()
        self.clock = FixedClock(NOW + timedelta(hours=1))
        self.transactions = InMemoryTransactionRunner()

    def wire[C](self, command: type[C]) -> C:
        return command(  # type: ignore[call-arg]
            orders=self.orders,
            stocks=self.stocks,
            transactions=self.transactions,
            events=self.events,
            clock=self.clock,
        )

    def notes(self) -> AddOrderNote:
        return AddOrderNote(orders=self.orders, transactions=self.transactions, clock=self.clock)

    async def store(
        self,
        status: OrderStatus = S.PENDING_PAYMENT,
        *,
        n: int = 1,
        method: DeliveryMethod = DeliveryMethod.HOME,
        reservation: bool = True,
        due: timedelta = timedelta(minutes=30),
    ) -> Order:
        """An order stored with number 1000 + n, its own id and reservation id (n)."""
        order = make_order(method, status, reservation=reservation)
        order.id = UUID(int=900 + n)
        order.number = 1000 + n
        order.reservation_id = UUID(int=70 + n) if reservation else None
        order.payment_due_at = NOW + due
        await self.orders.add(order)
        return order

    def current(self, n: int = 1) -> Order:
        found = next(o for o in self.orders.orders if o.number == 1000 + n)
        return found

    def history(self, n: int = 1) -> list[tuple[OrderStatus, OrderStatus, str]]:
        rows = self.orders.history.get(UUID(int=900 + n), [])
        return [(e.from_status, e.to_status, e.actor) for e in rows]

    def statuses(self) -> list[tuple[str, str]]:
        return [
            (e.from_status, e.to_status)
            for e in self.events.published
            if isinstance(e, OrderStatusChanged)
        ]


def ok(result: Result[None, DomainError]) -> None:
    assert isinstance(result, Ok), result


def refused(result: Result[None, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


def untouched(world: World, n: int = 1) -> None:
    """The order is as stored, with no history, and nothing was published."""
    assert world.history(n) == []
    assert world.events.published == []


# --- MarkOrderPaid -----------------------------------------------------------------------------


async def test_marking_paid_saves_the_order_commits_the_reservation_and_publishes() -> None:
    world = World()
    await world.store()

    result = await world.wire(MarkOrderPaid).execute(1001, "Transferencia directa", ADMIN)

    ok(result)
    stored = world.current()
    assert (stored.status, stored.updated_at) == (S.PAID, world.clock.now())
    assert world.stocks.operations == [("commit", UUID(int=71))]
    assert world.history() == [(S.PENDING_PAYMENT, S.PAID, ADMIN)]
    assert world.orders.history[UUID(int=901)][0].note == "Transferencia directa"
    assert world.statuses() == [("pending_payment", "paid")]


async def test_marking_paid_an_order_without_reservation_calls_no_inventory() -> None:
    world = World()
    await world.store(reservation=False)

    ok(await world.wire(MarkOrderPaid).execute(1001, "Efectivo", ADMIN))

    assert world.stocks.operations == []
    assert world.current().status is S.PAID


@pytest.mark.parametrize("code", ["INVENTORY_STOCK_UNAVAILABLE", "INVENTORY_STOCK_INSUFFICIENT"])
async def test_an_inventory_refusal_passes_through_and_leaves_the_order_unpaid(code: str) -> None:
    world = World(stocks=InMemoryStockHolds(refusal=refusal(code)))
    await world.store()

    error = refused(await world.wire(MarkOrderPaid).execute(1001, "Transferencia", ADMIN))

    assert error.code == code
    assert world.current().status is S.PENDING_PAYMENT
    untouched(world)


async def test_marking_paid_an_unknown_order_is_not_found_and_calls_nothing() -> None:
    world = World()

    error = refused(await world.wire(MarkOrderPaid).execute(1001, "x", ADMIN))

    assert error.code == "ORDERS_ORDER_NOT_FOUND"
    assert world.stocks.operations == []


async def test_marking_paid_a_paid_order_is_a_transition_error_and_calls_no_inventory() -> None:
    world = World()
    await world.store(S.PAID)

    error = refused(await world.wire(MarkOrderPaid).execute(1001, "x", ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert world.stocks.operations == []
    untouched(world)


async def test_marking_paid_with_a_blank_note_is_invalid_and_calls_no_inventory() -> None:
    world = World()
    await world.store()

    error = refused(await world.wire(MarkOrderPaid).execute(1001, "   ", ADMIN))

    assert error.code == "ORDERS_ORDER_DATA_INVALID"
    assert world.stocks.operations == []
    assert world.current().status is S.PENDING_PAYMENT


async def test_commands_find_the_order_by_its_number_among_several() -> None:
    world = World()
    await world.store(n=1)
    await world.store(n=2)

    ok(await world.wire(MarkOrderPaid).execute(1002, "x", ADMIN))

    assert (world.current(1).status, world.current(2).status) == (S.PENDING_PAYMENT, S.PAID)
    assert world.stocks.operations == [("commit", UUID(int=72))]


# --- AdvanceOrder ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "start", "to", "carrier", "tracking", "expected"),
    [
        (DeliveryMethod.HOME, S.PAID, "preparing", None, None, S.PREPARING),
        (DeliveryMethod.HOME, S.PAID, "shipped", "Estafeta", "123456", S.SHIPPED),
        (DeliveryMethod.LOCAL, S.PREPARING, "shipped", None, None, S.SHIPPED),
        (DeliveryMethod.PICKUP, S.PREPARING, "ready_for_pickup", None, None, S.READY_FOR_PICKUP),
        (DeliveryMethod.HOME, S.SHIPPED, "delivered", None, None, S.DELIVERED),
        (DeliveryMethod.PICKUP, S.READY_FOR_PICKUP, "delivered", None, None, S.DELIVERED),
    ],
)
async def test_advancing_dispatches_to_the_matching_move_without_touching_stock(
    method: DeliveryMethod,
    start: OrderStatus,
    to: str,
    carrier: str | None,
    tracking: str | None,
    expected: OrderStatus,
) -> None:
    world = World()
    await world.store(start, method=method)

    ok(await world.wire(AdvanceOrder).execute(1001, to, carrier, tracking, ADMIN))

    stored = world.current()
    assert stored.status is expected
    assert world.stocks.operations == []
    assert world.history() == [(start, expected, ADMIN)]
    assert world.statuses() == [(start.value, expected.value)]


async def test_shipping_stores_the_carrier_and_tracking_on_the_order_and_the_history() -> None:
    world = World()
    await world.store(S.PAID)

    ok(await world.wire(AdvanceOrder).execute(1001, "shipped", "Estafeta", "123456", ADMIN))

    stored = world.current()
    assert (stored.shipping_carrier, stored.tracking_number) == ("Estafeta", "123456")
    row = world.orders.history[UUID(int=901)][0]
    assert (row.carrier, row.tracking_number) == ("Estafeta", "123456")


async def test_shipping_a_home_order_without_data_is_invalid_and_saves_nothing() -> None:
    world = World()
    await world.store(S.PAID)

    error = refused(await world.wire(AdvanceOrder).execute(1001, "shipped", None, None, ADMIN))

    assert error.code == "ORDERS_ORDER_DATA_INVALID"
    assert world.current().status is S.PAID
    untouched(world)


async def test_shipping_a_pickup_order_is_a_transition_error() -> None:
    world = World()
    await world.store(S.PAID, method=DeliveryMethod.PICKUP)

    error = refused(await world.wire(AdvanceOrder).execute(1001, "shipped", None, None, ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert error.details == {"from": "paid", "to": "shipped"}


async def test_advancing_to_an_unknown_target_is_a_transition_error() -> None:
    world = World()
    await world.store(S.PAID)

    error = refused(await world.wire(AdvanceOrder).execute(1001, "paid", None, None, ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert error.details == {"from": "paid", "to": "paid"}
    untouched(world)


async def test_advancing_an_unknown_order_is_not_found() -> None:
    world = World()

    error = refused(await world.wire(AdvanceOrder).execute(1001, "delivered", None, None, ADMIN))

    assert error.code == "ORDERS_ORDER_NOT_FOUND"


async def test_moving_a_delivered_order_backwards_is_refused() -> None:
    world = World()
    await world.store(S.DELIVERED)

    error = refused(await world.wire(AdvanceOrder).execute(1001, "preparing", None, None, ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert world.current().status is S.DELIVERED


# --- CancelUnpaidOrder -------------------------------------------------------------------------


async def test_cancelling_unpaid_stores_the_reason_and_releases_the_reservation() -> None:
    world = World()
    await world.store()

    ok(await world.wire(CancelUnpaidOrder).execute(1001, "El cliente lo pidió", ADMIN))

    stored = world.current()
    assert (stored.status, stored.cancel_reason) == (S.CANCELLED, "El cliente lo pidió")
    assert stored.refund_status is None
    assert world.stocks.operations == [("release", UUID(int=71))]
    assert world.history() == [(S.PENDING_PAYMENT, S.CANCELLED, ADMIN)]
    assert world.statuses() == [("pending_payment", "cancelled")]


async def test_cancelling_an_unpaid_order_without_reservation_calls_no_inventory() -> None:
    world = World()
    await world.store(reservation=False)

    ok(await world.wire(CancelUnpaidOrder).execute(1001, "Motivo", ADMIN))

    assert world.stocks.operations == []
    assert world.current().status is S.CANCELLED


async def test_a_refused_release_leaves_the_order_pending() -> None:
    world = World(stocks=InMemoryStockHolds(refusal=refusal("INVENTORY_RESERVATION_COMMITTED")))
    await world.store()

    error = refused(await world.wire(CancelUnpaidOrder).execute(1001, "Motivo", ADMIN))

    assert error.code == "INVENTORY_RESERVATION_COMMITTED"
    assert world.current().status is S.PENDING_PAYMENT
    assert world.current().cancel_reason is None
    untouched(world)


async def test_cancelling_a_paid_order_through_the_unpaid_command_is_refused() -> None:
    world = World()
    await world.store(S.PAID)

    error = refused(await world.wire(CancelUnpaidOrder).execute(1001, "Motivo", ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert world.stocks.operations == []
    assert world.current().status is S.PAID


async def test_cancelling_unpaid_without_a_reason_is_invalid_and_releases_nothing() -> None:
    world = World()
    await world.store()

    error = refused(await world.wire(CancelUnpaidOrder).execute(1001, " ", ADMIN))

    assert error.code == "ORDERS_ORDER_DATA_INVALID"
    assert world.stocks.operations == []


async def test_cancelling_an_unknown_order_is_not_found() -> None:
    world = World()

    error = refused(await world.wire(CancelUnpaidOrder).execute(1001, "Motivo", ADMIN))

    assert error.code == "ORDERS_ORDER_NOT_FOUND"


# --- CancelPaidOrder ---------------------------------------------------------------------------


async def test_cancelling_a_paid_order_returns_the_units_and_leaves_the_refund_pending() -> None:
    world = World()
    await world.store(S.PAID)

    ok(await world.wire(CancelPaidOrder).execute(1001, "Sin producto", "owner-1"))

    stored = world.current()
    assert (stored.status, stored.refund_status) == (S.CANCELLED, RefundStatus.PENDING)
    assert stored.cancel_reason == "Sin producto"
    assert world.stocks.operations == [("return_sold", UUID(int=71))]
    assert world.history() == [(S.PAID, S.CANCELLED, "owner-1")]
    assert world.statuses() == [("paid", "cancelled")]


async def test_cancelling_a_paid_order_without_reservation_calls_no_inventory() -> None:
    world = World()
    await world.store(S.PAID, reservation=False)

    ok(await world.wire(CancelPaidOrder).execute(1001, "Motivo", ADMIN))

    assert world.stocks.operations == []
    assert world.current().refund_status is RefundStatus.PENDING


async def test_a_refused_return_leaves_the_paid_order_untouched() -> None:
    world = World(stocks=InMemoryStockHolds(refusal=refusal("INVENTORY_RESERVATION_NOT_COMMITTED")))
    await world.store(S.PAID)

    error = refused(await world.wire(CancelPaidOrder).execute(1001, "Motivo", ADMIN))

    assert error.code == "INVENTORY_RESERVATION_NOT_COMMITTED"
    stored = world.current()
    assert (stored.status, stored.refund_status, stored.cancel_reason) == (S.PAID, None, None)
    untouched(world)


@pytest.mark.parametrize("start", [S.PENDING_PAYMENT, S.PREPARING, S.SHIPPED, S.CANCELLED])
async def test_cancel_paid_refuses_any_other_status_without_calling_inventory(
    start: OrderStatus,
) -> None:
    world = World()
    await world.store(start)

    error = refused(await world.wire(CancelPaidOrder).execute(1001, "Motivo", ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert world.stocks.operations == []


async def test_cancel_paid_of_an_unknown_order_is_not_found() -> None:
    error = refused(await World().wire(CancelPaidOrder).execute(1001, "Motivo", ADMIN))

    assert error.code == "ORDERS_ORDER_NOT_FOUND"


# --- MarkOrderRefunded -------------------------------------------------------------------------


async def test_marking_the_refund_done_keeps_the_order_cancelled_and_touches_no_stock() -> None:
    world = World()
    await world.store(S.PAID)
    ok(await world.wire(CancelPaidOrder).execute(1001, "Motivo", "owner-1"))
    world.stocks.operations.clear()
    world.events.published.clear()

    ok(await world.wire(MarkOrderRefunded).execute(1001, "Reembolso por transferencia", "owner-1"))

    stored = world.current()
    assert (stored.status, stored.refund_status) == (S.CANCELLED, RefundStatus.DONE)
    assert world.stocks.operations == []
    assert world.history()[-1] == (S.CANCELLED, S.CANCELLED, "owner-1")
    assert world.orders.history[UUID(int=901)][-1].note == "Reembolso por transferencia"
    # `InMemoryOrders.save` keeps a copy that still carries the earlier recorded event (the SQL
    # adapter has no such thing), so only the last published event is the refund mark.
    assert world.statuses()[-1] == ("cancelled", "cancelled")


async def test_a_second_refund_mark_is_a_transition_error() -> None:
    world = World()
    await world.store(S.PAID)
    ok(await world.wire(CancelPaidOrder).execute(1001, "Motivo", ADMIN))
    ok(await world.wire(MarkOrderRefunded).execute(1001, "Hecho", ADMIN))
    rows = len(world.orders.history[UUID(int=901)])

    error = refused(await world.wire(MarkOrderRefunded).execute(1001, "Otra vez", ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert len(world.orders.history[UUID(int=901)]) == rows


async def test_the_refund_of_an_order_that_is_not_cancelled_is_refused() -> None:
    world = World()
    await world.store(S.PAID)

    error = refused(await world.wire(MarkOrderRefunded).execute(1001, "Hecho", ADMIN))

    assert error.code == "ORDERS_TRANSITION_INVALID"


async def test_the_refund_of_an_unknown_order_is_not_found() -> None:
    error = refused(await World().wire(MarkOrderRefunded).execute(1001, "Hecho", ADMIN))

    assert error.code == "ORDERS_ORDER_NOT_FOUND"


# --- AddOrderNote ------------------------------------------------------------------------------


async def test_adding_a_note_stores_it_with_author_and_time_and_changes_nothing_else() -> None:
    world = World()
    await world.store()

    ok(await world.notes().execute(1001, "  Llamar al cliente\ncon calma ", ADMIN))

    assert world.orders.notes[UUID(int=901)] == [
        (world.clock.now(), ADMIN, "Llamar al cliente\ncon calma")
    ]
    assert world.current().status is S.PENDING_PAYMENT
    assert world.current().updated_at == NOW
    untouched(world)
    assert world.stocks.operations == []


async def test_notes_accumulate_in_order() -> None:
    world = World()
    await world.store()

    ok(await world.notes().execute(1001, "uno", "a"))
    ok(await world.notes().execute(1001, "dos", "b"))

    assert [(actor, text) for _, actor, text in world.orders.notes[UUID(int=901)]] == [
        ("a", "uno"),
        ("b", "dos"),
    ]


async def test_a_note_on_an_unknown_order_is_not_found() -> None:
    world = World()

    error = refused(await world.notes().execute(1001, "hola", ADMIN))

    assert error.code == "ORDERS_ORDER_NOT_FOUND"
    assert world.orders.notes == {}


@pytest.mark.parametrize("text", ["", "   \n", "x" * 1001])
async def test_a_blank_or_oversized_note_is_invalid_and_stores_nothing(text: str) -> None:
    world = World()
    await world.store()

    error = refused(await world.notes().execute(1001, text, ADMIN))

    assert error.code == "ORDERS_ORDER_DATA_INVALID"
    assert (error.details or {})["field"] == "text"
    assert world.orders.notes == {}


async def test_a_note_validates_before_looking_the_order_up() -> None:
    """Blank text on an unknown number reports the text, not the missing order."""
    error = refused(await World().notes().execute(1001, "", ADMIN))

    assert error.code == "ORDERS_ORDER_DATA_INVALID"


# --- CancelOverdueOrders -----------------------------------------------------------------------


async def test_the_job_cancels_overdue_unpaid_orders_with_the_system_reason() -> None:
    world = World()
    await world.store(n=1, due=timedelta(minutes=-10))  # before the clock (NOW + 1 h)

    count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 1
    stored = world.current()
    assert (stored.status, stored.cancel_reason) == (S.CANCELLED, "No pagado a tiempo")
    assert stored.refund_status is None
    assert world.history() == [(S.PENDING_PAYMENT, S.CANCELLED, "system")]
    assert world.orders.history[UUID(int=901)][0].note == "No pagado a tiempo"
    assert world.stocks.operations == [("release", UUID(int=71))]
    assert world.statuses() == [("pending_payment", "cancelled")]


async def test_the_job_leaves_orders_that_are_not_overdue_or_not_unpaid() -> None:
    world = World()
    await world.store(n=1, due=timedelta(hours=2))  # still inside its window
    await world.store(S.PAID, n=2, due=timedelta(minutes=-10))
    await world.store(S.CANCELLED, n=3, due=timedelta(minutes=-10))

    count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 0
    assert [o.status for o in world.orders.orders] == [S.PENDING_PAYMENT, S.PAID, S.CANCELLED]
    assert world.stocks.operations == []
    assert world.events.published == []


async def test_the_job_cancels_several_and_counts_them() -> None:
    world = World()
    await world.store(n=1, due=timedelta(minutes=-30))
    await world.store(n=2, due=timedelta(minutes=-20))
    await world.store(n=3, due=timedelta(hours=3))

    count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 2
    assert [o.status for o in world.orders.orders] == [S.CANCELLED, S.CANCELLED, S.PENDING_PAYMENT]
    assert world.stocks.operations == [("release", UUID(int=71)), ("release", UUID(int=72))]
    assert len(world.events.published) == 2


async def test_the_job_calls_no_inventory_for_an_order_without_reservation() -> None:
    world = World()
    await world.store(reservation=False, due=timedelta(minutes=-1))

    count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 1
    assert world.stocks.operations == []
    assert world.current().status is S.CANCELLED


async def test_the_job_with_nothing_overdue_returns_zero() -> None:
    assert await World().wire(CancelOverdueOrders).run_once() == 0


async def test_the_job_claims_a_batch_of_100() -> None:
    world = World()
    claimed: list[int] = []
    original = world.orders.claim_overdue

    async def spy(now, limit):  # type: ignore[no-untyped-def]
        claimed.append(limit)
        return await original(now, limit)

    world.orders.claim_overdue = spy  # type: ignore[method-assign]

    await world.wire(CancelOverdueOrders).run_once()

    assert OVERDUE_BATCH == 100
    assert claimed == [100]


class SelectiveHolds(InMemoryStockHolds):
    """Refuses the release of the given reservation ids only."""

    def __init__(self, refuse: set[UUID], code: str) -> None:
        super().__init__()
        self._refuse = refuse
        self._code = code

    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        self.operations.append(("release", reservation_id))
        if reservation_id in self._refuse:
            return Err(refusal(self._code))
        return Ok(None)


async def test_deviation_5_a_refused_release_skips_and_logs_that_order_and_cancels_the_rest() -> (
    None
):
    holds = SelectiveHolds({UUID(int=72)}, "INVENTORY_RESERVATION_COMMITTED")
    world = World(stocks=holds)
    await world.store(n=1, due=timedelta(minutes=-30))
    await world.store(n=2, due=timedelta(minutes=-20))
    await world.store(n=3, due=timedelta(minutes=-10))

    with capture_logs() as logs:
        count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 2
    assert [o.status for o in world.orders.orders] == [S.CANCELLED, S.PENDING_PAYMENT, S.CANCELLED]
    # The skipped order has no history row, no event and keeps its reason empty.
    assert world.history(2) == []
    assert world.current(2).cancel_reason is None
    assert [e.number for e in world.events.published if isinstance(e, OrderStatusChanged)] == [
        1001,
        1003,
    ]
    warnings = [log for log in logs if log["event"] == "orders.overdue_release_refused"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert (warnings[0]["number"], warnings[0]["code"]) == (
        1002,
        "INVENTORY_RESERVATION_COMMITTED",
    )
    # Every order was still tried, in claim order.
    assert holds.operations == [
        ("release", UUID(int=71)),
        ("release", UUID(int=72)),
        ("release", UUID(int=73)),
    ]


class StaleClaim(InMemoryOrders):
    """Claims an order that stopped being pending after it was read (the aggregate refuses)."""

    async def claim_overdue(self, now, limit):  # type: ignore[no-untyped-def]
        claimed = await super().claim_overdue(now, limit)
        for order in claimed:
            if order.number == 1002:
                order.status = OrderStatus.PAID
        return claimed


async def test_deviation_5_a_refused_cancel_skips_and_logs_that_order_without_releasing_it() -> (
    None
):
    world = World(orders=StaleClaim())
    await world.store(n=1, due=timedelta(minutes=-30))
    await world.store(n=2, due=timedelta(minutes=-20))

    with capture_logs() as logs:
        count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 1
    assert world.stocks.operations == [("release", UUID(int=71))]
    assert world.current(2).status is S.PENDING_PAYMENT  # the store keeps what was saved
    assert world.history(2) == []
    warnings = [log for log in logs if log["event"] == "orders.overdue_cancel_refused"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert (warnings[0]["number"], warnings[0]["code"]) == (1002, "ORDERS_TRANSITION_INVALID")


async def test_deviation_5_when_every_release_is_refused_the_job_cancels_none() -> None:
    world = World(stocks=InMemoryStockHolds(refusal=refusal()))
    await world.store(n=1, due=timedelta(minutes=-30))
    await world.store(n=2, due=timedelta(minutes=-20))

    with capture_logs() as logs:
        count = await world.wire(CancelOverdueOrders).run_once()

    assert count == 0
    assert [o.status for o in world.orders.orders] == [S.PENDING_PAYMENT, S.PENDING_PAYMENT]
    assert world.events.published == []
    assert [log["event"] for log in logs] == ["orders.overdue_release_refused"] * 2
