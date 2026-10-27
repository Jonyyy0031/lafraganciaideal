"""Payments plan 002 step 2, application: `OrderPaymentConfirmations.confirm` (confirm_payment.py)
over the in-memory orders and stock holds: every outcome, what each leaves behind (status, history,
inventory calls, events) and the lookup by id (`get_by_id_for_update`). The real commit / release
and the lock are in the integration tests."""

from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.orders import (
    OrderPaymentConfirmations as PublicFacade,
)
from fragancia_api.modules.orders import (
    PaymentConfirmation as PublicConfirmation,
)
from fragancia_api.modules.orders import (
    PaymentConfirmationOutcome as PublicOutcome,
)
from fragancia_api.modules.orders.application.commands.confirm_payment import (
    PAID_NOTE,
    STOCK_GONE_REASON,
    OrderPaymentConfirmations,
    PaymentConfirmation,
    PaymentConfirmationOutcome,
)
from fragancia_api.modules.orders.domain.order import (
    PAYMENTS_ACTOR,
    SYSTEM_ACTOR,
    OrderPaid,
    OrderStatus,
    OrderStatusChanged,
    cancelled_by,
)
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryStockHolds
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.orders.test_order_lifecycle import World, refusal

S = OrderStatus
O = PaymentConfirmationOutcome  # noqa: E741
PAYMENT_ID = "555000111"
ORDER_ID = UUID(int=901)  # World.store(n=1)


class CommitRefusing(InMemoryStockHolds):
    """Refuses only the commit; release works."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self._code = code

    async def commit(self, reservation_id: UUID) -> Result[None, DomainError]:
        self.operations.append(("commit", reservation_id))
        return Err(refusal(self._code))


class ReleaseRefusing(CommitRefusing):
    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        self.operations.append(("release", reservation_id))
        return Err(ConflictError(code="INVENTORY_RESERVATION_COMMITTED", message="refused"))


async def confirm(
    world: World,
    *,
    order_id: UUID = ORDER_ID,
    amount: int | None = None,
    currency: str = "MXN",
) -> Result[PaymentConfirmation, DomainError]:
    total = world.current().total_cents if amount is None else amount
    return await world.wire(OrderPaymentConfirmations).confirm(
        order_id, PAYMENT_ID, total, currency
    )


def outcome_of(result: Result[PaymentConfirmation, DomainError]) -> PaymentConfirmation:
    assert isinstance(result, Ok), result
    return result.value


def untouched(world: World) -> None:
    assert world.history() == []
    assert world.events.published == []


def test_the_facade_and_its_types_are_exported_by_the_orders_module() -> None:
    assert PublicFacade is OrderPaymentConfirmations
    assert PublicConfirmation is PaymentConfirmation
    assert PublicOutcome is PaymentConfirmationOutcome
    assert [o.value for o in PaymentConfirmationOutcome] == [
        "paid",
        "not_pending",
        "amount_mismatch",
        "stock_gone",
        "not_found",
    ]


# --- paid --------------------------------------------------------------------------------------


async def test_a_matching_payment_commits_the_reservation_and_marks_the_order_paid() -> None:
    world = World()
    await world.store()

    result = await confirm(world)

    assert outcome_of(result) == PaymentConfirmation(O.PAID, 1001)
    stored = world.current()
    assert (stored.status, stored.updated_at) == (S.PAID, world.clock.now())
    assert world.stocks.operations == [("commit", UUID(int=71))]
    assert world.history() == [(S.PENDING_PAYMENT, S.PAID, SYSTEM_ACTOR)]
    assert world.orders.history[ORDER_ID][0].note == PAID_NOTE.format(payment_id=PAYMENT_ID)
    assert PAID_NOTE.format(payment_id=PAYMENT_ID) == "Pagado con Mercado Pago (pago 555000111)"


async def test_the_paid_move_publishes_the_status_change_and_the_paid_event() -> None:
    world = World()
    await world.store()

    await confirm(world)

    assert world.statuses() == [("pending_payment", "paid")]
    assert [e.number for e in world.events.published if isinstance(e, OrderPaid)] == [1001]


async def test_an_order_without_a_reservation_is_paid_without_calling_inventory() -> None:
    world = World()
    await world.store(reservation=False)

    result = await confirm(world)

    assert outcome_of(result).outcome is O.PAID
    assert world.stocks.operations == []
    assert world.current().status is S.PAID


async def test_a_payment_after_the_deadline_still_pays_while_the_units_can_be_sold() -> None:
    world = World()
    await world.store(due=-timedelta(hours=3))

    result = await confirm(world)

    assert outcome_of(result).outcome is O.PAID
    assert world.current().status is S.PAID
    assert world.stocks.operations == [("commit", UUID(int=71))]


async def test_each_order_is_found_by_its_own_id() -> None:
    world = World()
    await world.store(n=1)
    await world.store(n=2)

    result = await confirm(world, order_id=UUID(int=902), amount=world.current(2).total_cents)

    assert outcome_of(result) == PaymentConfirmation(O.PAID, 1002)
    assert world.current(1).status is S.PENDING_PAYMENT
    assert world.current(2).status is S.PAID
    assert world.stocks.operations == [("commit", UUID(int=72))]


# --- not found and not pending -----------------------------------------------------------------


async def test_an_unknown_order_id_is_not_found_and_calls_nothing() -> None:
    world = World()
    await world.store()

    result = await confirm(world, order_id=UUID(int=12345), amount=1)

    assert outcome_of(result) == PaymentConfirmation(O.NOT_FOUND)
    assert world.stocks.operations == []
    untouched(world)


@pytest.mark.parametrize(
    "status",
    [S.PAID, S.PREPARING, S.SHIPPED, S.READY_FOR_PICKUP, S.DELIVERED, S.CANCELLED],
)
async def test_an_order_that_is_not_pending_is_reported_with_its_status_and_not_touched(
    status: OrderStatus,
) -> None:
    world = World()
    await world.store(status)

    result = await confirm(world)

    assert outcome_of(result) == PaymentConfirmation(O.NOT_PENDING, 1001, status.value)
    assert world.current().status is status
    assert world.stocks.operations == []
    untouched(world)


async def test_a_second_payment_for_a_paid_order_is_not_pending() -> None:
    world = World()
    await world.store()
    await confirm(world)
    ops_after_first = list(world.stocks.operations)

    second = await confirm(world)

    assert outcome_of(second) == PaymentConfirmation(O.NOT_PENDING, 1001, "paid")
    assert world.stocks.operations == ops_after_first
    assert len(world.history()) == 1


async def test_the_status_check_comes_before_the_amount_check() -> None:
    world = World()
    await world.store(S.CANCELLED)

    result = await confirm(world, amount=1, currency="USD")

    assert outcome_of(result).outcome is O.NOT_PENDING


# --- amount and currency -----------------------------------------------------------------------


async def test_another_amount_is_a_mismatch_and_changes_nothing() -> None:
    world = World()
    await world.store()
    total = world.current().total_cents

    result = await confirm(world, amount=total - 1)

    assert outcome_of(result) == PaymentConfirmation(O.AMOUNT_MISMATCH, 1001)
    assert world.current().status is S.PENDING_PAYMENT
    assert world.stocks.operations == []
    untouched(world)


async def test_an_amount_above_the_total_is_a_mismatch_too() -> None:
    world = World()
    await world.store()

    result = await confirm(world, amount=world.current().total_cents + 1)

    assert outcome_of(result).outcome is O.AMOUNT_MISMATCH
    untouched(world)


@pytest.mark.parametrize("currency", ["USD", "mxn", "", "MXN "])
async def test_another_currency_is_a_mismatch_even_with_the_right_amount(currency: str) -> None:
    world = World()
    await world.store()

    result = await confirm(world, currency=currency)

    assert outcome_of(result) == PaymentConfirmation(O.AMOUNT_MISMATCH, 1001)
    assert world.stocks.operations == []
    untouched(world)


# --- the units are gone ------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["INVENTORY_STOCK_UNAVAILABLE", "INVENTORY_STOCK_INSUFFICIENT"])
async def test_units_that_can_no_longer_be_sold_cancel_the_order_and_release_the_reservation(
    code: str,
) -> None:
    holds = CommitRefusing(code)
    world = World(stocks=holds)
    await world.store()

    result = await confirm(world)

    assert outcome_of(result) == PaymentConfirmation(O.STOCK_GONE, 1001)
    stored = world.current()
    assert (stored.status, stored.updated_at) == (S.CANCELLED, world.clock.now())
    assert stored.cancel_reason == STOCK_GONE_REASON.format(payment_id=PAYMENT_ID)
    assert STOCK_GONE_REASON.format(payment_id=PAYMENT_ID) == (
        "Pago recibido sin existencias (pago 555000111)"
    )
    assert holds.operations == [("commit", UUID(int=71)), ("release", UUID(int=71))]
    assert world.history() == [(S.PENDING_PAYMENT, S.CANCELLED, PAYMENTS_ACTOR)]
    assert world.statuses() == [("pending_payment", "cancelled")]
    assert [e for e in world.events.published if isinstance(e, OrderPaid)] == []


async def test_a_cancelled_for_stock_order_is_cancelled_by_the_system() -> None:
    world = World(stocks=CommitRefusing("INVENTORY_STOCK_UNAVAILABLE"))
    await world.store()

    await confirm(world)

    changed = [e for e in world.events.published if isinstance(e, OrderStatusChanged)]
    assert [(e.from_status, e.to_status) for e in changed] == [("pending_payment", "cancelled")]
    assert cancelled_by(world.history()[0][2]) == "system"


@pytest.mark.parametrize(
    "code", ["INVENTORY_RESERVATION_COMMITTED", "INVENTORY_RESERVATION_NOT_FOUND", "OTHER"]
)
async def test_any_other_commit_refusal_passes_through_and_leaves_the_order_pending(
    code: str,
) -> None:
    holds = CommitRefusing(code)
    world = World(stocks=holds)
    await world.store()

    result = await confirm(world)

    assert isinstance(result, Err)
    assert result.error.code == code
    assert world.current().status is S.PENDING_PAYMENT
    assert holds.operations == [("commit", UUID(int=71))]
    untouched(world)


async def test_a_refused_release_after_a_gone_commit_passes_through_without_saving() -> None:
    holds = ReleaseRefusing("INVENTORY_STOCK_UNAVAILABLE")
    world = World(stocks=holds)
    await world.store()

    result = await confirm(world)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_COMMITTED"
    assert holds.operations == [("commit", UUID(int=71)), ("release", UUID(int=71))]
    # The order was cancelled in memory only: it was never saved nor published (the real
    # transaction rolls the whole thing back).
    assert world.current().status is S.PENDING_PAYMENT
    untouched(world)


# --- transaction -------------------------------------------------------------------------------


async def test_the_work_runs_in_one_transaction_of_the_caller() -> None:
    world = World()
    await world.store()
    runs: list[int] = []
    original = world.transactions.run

    async def counting(work):  # type: ignore[no-untyped-def]
        runs.append(1)
        return await original(work)

    world.transactions.run = counting  # type: ignore[method-assign]

    await confirm(world)

    assert runs == [1]
