"""Payments plan 003 step 3, application: `OrderPaymentConfirmations.hold_for_payment`
(confirm_payment.py) over the in-memory orders and stock holds: the deadline moved and the
reservation extended, not moved, an order without a reservation, an extension that inventory
refuses (logged, the deadline stays), not pending, not found, and what is published. The real
`extend` of inventory and the one transaction are in the integration tests."""

from datetime import timedelta
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.orders.application.commands.confirm_payment import (
    OrderPaymentConfirmations,
    PaymentConfirmation,
    PaymentConfirmationOutcome,
)
from fragancia_api.modules.orders.domain.order import (
    OrderPaymentPending,
    OrderStatus,
    OrderStatusChanged,
)
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryStockHolds
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW
from tests.unit.orders.test_confirm_payment import ORDER_ID
from tests.unit.orders.test_order_lifecycle import World

S = OrderStatus
O = PaymentConfirmationOutcome  # noqa: E741
URL = "https://mp.test/ticket/1"


async def hold(
    world: World,
    *,
    until_after_due: timedelta = timedelta(hours=47),
    order_id: UUID = ORDER_ID,
    method: str = "efectivo en tienda",
    url: str | None = URL,
) -> Result[PaymentConfirmation, DomainError]:
    due = world.current().payment_due_at if world.orders.orders else NOW
    return await world.wire(OrderPaymentConfirmations).hold_for_payment(
        order_id, due + until_after_due, due + until_after_due, method, url
    )


def outcome_of(result: Result[PaymentConfirmation, DomainError]) -> PaymentConfirmation:
    assert isinstance(result, Ok), result
    return result.value


def payment_pending(world: World) -> list[OrderPaymentPending]:
    return [e for e in world.events.published if isinstance(e, OrderPaymentPending)]


async def test_a_later_ticket_moves_the_deadline_and_extends_the_reservation_to_it() -> None:
    world = World()
    await world.store()
    until = world.current().payment_due_at + timedelta(hours=47)

    result = await hold(world)

    assert outcome_of(result) == PaymentConfirmation(O.HELD, 1001)
    stored = world.current()
    assert stored.payment_due_at == until
    assert stored.status is S.PENDING_PAYMENT
    assert world.stocks.extensions == [(UUID(int=71), until)]
    assert world.stocks.operations == []


async def test_it_publishes_only_the_payment_pending_event_with_the_new_deadline() -> None:
    world = World()
    await world.store()
    until = world.current().payment_due_at + timedelta(hours=47)

    await hold(world, method="transferencia SPEI", url=None)

    [event] = world.events.published
    assert isinstance(event, OrderPaymentPending)
    assert (event.order_id, event.number) == (ORDER_ID, 1001)
    assert (event.due_at, event.method, event.instructions_url) == (
        until,
        "transferencia SPEI",
        None,
    )
    assert world.statuses() == []
    assert world.history() == []


async def test_an_earlier_ticket_keeps_the_deadline_and_still_publishes() -> None:
    world = World()
    await world.store()
    before = world.current().payment_due_at

    result = await hold(world, until_after_due=-timedelta(minutes=10))

    assert outcome_of(result).outcome is O.HELD
    assert world.current().payment_due_at == before
    assert world.stocks.extensions == []
    [event] = payment_pending(world)
    assert event.due_at == before


async def test_a_ticket_expiring_exactly_at_the_deadline_does_not_extend() -> None:
    world = World()
    await world.store()

    await hold(world, until_after_due=timedelta(0))

    assert world.stocks.extensions == []
    assert len(payment_pending(world)) == 1


async def test_a_second_later_ticket_moves_it_again() -> None:
    world = World()
    await world.store()
    first = world.current().payment_due_at + timedelta(hours=40)
    second = world.current().payment_due_at + timedelta(hours=47)

    facade = world.wire(OrderPaymentConfirmations)
    await facade.hold_for_payment(ORDER_ID, first, first, "efectivo en tienda", None)
    await facade.hold_for_payment(ORDER_ID, second, second, "transferencia SPEI", None)
    await facade.hold_for_payment(ORDER_ID, first, first, "efectivo en tienda", None)

    assert world.current().payment_due_at == second
    # The third ticket expires earlier than the deadline: no shortening, no extension.
    assert world.stocks.extensions == [(UUID(int=71), first), (UUID(int=71), second)]
    assert len(payment_pending(world)) == 3


async def test_an_order_without_a_reservation_moves_the_deadline_without_calling_inventory() -> (
    None
):
    world = World()
    await world.store(reservation=False)
    until = world.current().payment_due_at + timedelta(hours=47)

    result = await hold(world)

    assert outcome_of(result).outcome is O.HELD
    assert world.current().payment_due_at == until
    assert world.stocks.extensions == []


async def test_a_refused_extension_is_logged_and_the_new_deadline_and_event_stay() -> None:
    refusal = ConflictError(code="INVENTORY_RESERVATION_EXPIRED", message="refused")
    world = World(stocks=InMemoryStockHolds(extend_refusal=refusal))
    await world.store()
    until = world.current().payment_due_at + timedelta(hours=47)

    with capture_logs() as logs:
        result = await hold(world)

    assert outcome_of(result) == PaymentConfirmation(O.HELD, 1001)
    assert world.current().payment_due_at == until
    [event] = payment_pending(world)
    assert event.due_at == until
    [log] = [e for e in logs if e["event"] == "orders.payment_hold_not_extended"]
    assert log["number"] == 1001
    assert log["reservation_id"] == str(UUID(int=71))
    assert log["code"] == "INVENTORY_RESERVATION_EXPIRED"


@pytest.mark.parametrize(
    "status",
    [S.PAID, S.PREPARING, S.SHIPPED, S.READY_FOR_PICKUP, S.DELIVERED, S.CANCELLED],
)
async def test_an_order_that_is_not_pending_answers_not_pending_with_its_status_and_changes_nothing(
    status: OrderStatus,
) -> None:
    world = World()
    await world.store(status)
    before = world.current().payment_due_at

    result = await hold(world)

    assert outcome_of(result) == PaymentConfirmation(O.NOT_PENDING, 1001, status.value)
    assert world.current().payment_due_at == before
    assert world.stocks.extensions == []
    assert world.events.published == []
    assert world.history() == []


async def test_an_unknown_order_is_not_found_and_calls_nothing() -> None:
    world = World()

    result = await hold(world, order_id=UUID(int=4242))

    assert outcome_of(result) == PaymentConfirmation(O.NOT_FOUND)
    assert world.stocks.extensions == []
    assert world.events.published == []


async def test_the_hold_never_commits_or_releases_the_reservation() -> None:
    world = World()
    await world.store()

    await hold(world)

    assert [kind for kind, _ in world.stocks.operations] == []
    assert all(not isinstance(e, OrderStatusChanged) for e in world.events.published)


async def test_a_refusal_from_the_order_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The facade only reaches `await_payment` for a pending order, so an `Err` there is a
    domain refusal it must not swallow."""
    world = World()
    await world.store()
    refusal = ConflictError(code="ORDERS_TRANSITION_INVALID", message="refused")

    def refuse(*_: object) -> Err[DomainError]:
        return Err(refusal)

    monkeypatch.setattr("fragancia_api.modules.orders.domain.order.Order.await_payment", refuse)

    result = await hold(world)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_TRANSITION_INVALID"
    assert world.stocks.extensions == []
    assert world.events.published == []
