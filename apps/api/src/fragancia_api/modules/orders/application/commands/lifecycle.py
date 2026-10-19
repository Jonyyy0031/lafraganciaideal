"""Commands of the order lifecycle (decisions 9, 10, 19, 24 and 32 to 38).

Each state change runs in one `transactions.run`: lock the order row (`get_by_number_for_update`),
make the move on the aggregate, call inventory when the move needs it (the order row first, then
inventory's own lock order), save and publish the order's events. When the move or inventory
refuses, the whole transaction rolls back and the refusal passes through.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

import structlog

from fragancia_api.modules.orders.application.ports import StockHolds
from fragancia_api.modules.orders.domain.errors import OrderNotFound, OrderTransitionInvalid
from fragancia_api.modules.orders.domain.order import (
    AUTO_CANCEL_REASON,
    SYSTEM_ACTOR,
    Order,
    clean_note_text,
)
from fragancia_api.modules.orders.domain.repositories import OrderRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

OVERDUE_BATCH = 100

type _Move = Callable[[Order, datetime], Result[None, DomainError]]
type _StockCall = Callable[[UUID], Awaitable[Result[None, DomainError]]]


class OrderMove:
    """What every state-changing command shares: the locked load, the move, the optional
    inventory call, the save and the events."""

    def __init__(
        self,
        *,
        orders: OrderRepository,
        stocks: StockHolds,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._orders = orders
        self._stocks = stocks
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def _apply(
        self, number: int, move: _Move, stock: _StockCall | None
    ) -> Result[None, DomainError]:
        """`stock` runs only for an order that holds a reservation (made-to-order orders have
        none)."""

        async def work() -> Result[None, DomainError]:
            order = await self._orders.get_by_number_for_update(number)
            if order is None:
                return Err(OrderNotFound())
            match move(order, self._clock.now()):
                case Err(refused):
                    return Err(refused)
                case Ok(_):
                    pass
            if stock is not None and order.reservation_id is not None:
                match await stock(order.reservation_id):
                    case Err(refused):
                        return Err(refused)
                    case Ok(_):
                        pass
            await self._orders.save(order)
            await self._events.publish(order.pull_events())
            return Ok(None)

        return await self._transactions.run(work)


class MarkOrderPaid(OrderMove):
    """Command: the order was paid by hand (decision 32), owners only. Commits the reservation,
    which sells the units. A refusal of inventory (`INVENTORY_STOCK_UNAVAILABLE` or
    `INVENTORY_STOCK_INSUFFICIENT`, e.g. an expired reservation whose units are gone) passes
    through as is and the order stays `pending_payment`."""

    async def execute(self, number: int, note: str, actor: str) -> Result[None, DomainError]:
        return await self._apply(
            number, lambda order, now: order.mark_paid(note, actor, now), self._stocks.commit
        )


class AdvanceOrder(OrderMove):
    """Command: move a paid order forward: preparing, shipped, ready for pickup or delivered
    (decisions 9, 33 and 35). Stock is not touched."""

    async def execute(
        self,
        number: int,
        to: str,
        carrier: str | None,
        tracking_number: str | None,
        actor: str,
    ) -> Result[None, DomainError]:
        def move(order: Order, now: datetime) -> Result[None, DomainError]:
            match to:
                case "preparing":
                    return order.start_preparing(actor, now)
                case "shipped":
                    return order.ship(carrier, tracking_number, actor, now)
                case "ready_for_pickup":
                    return order.ready_for_pickup(actor, now)
                case "delivered":
                    return order.deliver(actor, now)
                case _:
                    return Err(
                        OrderTransitionInvalid(
                            details={"from": order.status.value, "to": to},
                        )
                    )

        return await self._apply(number, move, None)


class CancelUnpaidOrder(OrderMove):
    """Command: cancel an order that is still `pending_payment` (decision 10). Releases its
    reservation, which is idempotent on an expired one."""

    async def execute(self, number: int, reason: str, actor: str) -> Result[None, DomainError]:
        return await self._apply(
            number,
            lambda order, now: order.cancel_unpaid(reason, actor, now),
            self._stocks.release,
        )


class CancelPaidOrder(OrderMove):
    """Command: cancel a paid order, owners only (decisions 34 and 36). Returns the units
    through inventory's `return_sold` and leaves the refund pending."""

    async def execute(self, number: int, reason: str, actor: str) -> Result[None, DomainError]:
        return await self._apply(
            number,
            lambda order, now: order.cancel_paid(reason, actor, now),
            self._stocks.return_sold,
        )


class MarkOrderRefunded(OrderMove):
    """Command: the refund of a cancelled paid order was made by hand (decision 36), owners
    only. Stock is not touched."""

    async def execute(self, number: int, note: str, actor: str) -> Result[None, DomainError]:
        return await self._apply(
            number, lambda order, now: order.mark_refunded(note, actor, now), None
        )


class AddOrderNote:
    """Command: an internal note on an order (decision 37). It goes into its own table and only
    takes `FOR KEY SHARE` on the order through its foreign key, which does not conflict with the
    `FOR NO KEY UPDATE` of a move, so it never waits."""

    def __init__(
        self, *, orders: OrderRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._orders = orders
        self._transactions = transactions
        self._clock = clock

    async def execute(self, number: int, text: str, actor: str) -> Result[None, DomainError]:
        match clean_note_text(text):
            case Err(invalid):
                return Err(invalid)
            case Ok(clean):
                pass

        async def work() -> Result[None, DomainError]:
            if not await self._orders.add_note(number, actor, clean, self._clock.now()):
                return Err(OrderNotFound())
            return Ok(None)

        return await self._transactions.run(work)


class CancelOverdueOrders(OrderMove):
    """Worker job (decision 24): cancel the unpaid orders whose `payment_due_at` passed and
    release their reservations (already expired in most cases). The actor is `system`.
    Returns how many orders it cancelled.

    One transaction per order: each claims a single overdue order (`SKIP LOCKED`), cancels it,
    releases its reservation, saves and publishes. A transaction therefore holds the stock items
    of one reservation only, taken in id order, which keeps inventory's global lock order
    (`inventory/application/commands/reservations.py:1-20`). A whole batch in one transaction
    would hold the items of order A while asking for those of order B and could deadlock against
    checkout, a manual move or `ExpireReservations`. The run stops when nothing is claimed,
    after `OVERDUE_BATCH` orders, or at the first order whose cancel or release is refused
    (logged).

    Such a refused order stays `pending_payment`, so it is re-claimed (and stops the run) every
    time, which would starve the newer overdue orders behind it. Today release refuses only a
    committed, returned or missing reservation, and commit happens atomically with `paid`, so
    it cannot happen. It can once the payments initiative commits reservations from a webhook;
    that initiative must handle it (initiative README, "Dependency notes")."""

    async def run_once(self) -> int:
        cancelled = 0
        for _ in range(OVERDUE_BATCH):
            match await self._transactions.run(self._cancel_next):
                case Err(_):
                    break
                case Ok(True):
                    cancelled += 1
                case Ok(_):
                    break
        return cancelled

    async def _cancel_next(self) -> Result[bool, DomainError]:
        """True when it cancelled an order; False when nothing is overdue or the claimed order
        was refused (logged, left as it was)."""
        now = self._clock.now()
        claimed = await self._orders.claim_overdue(now, 1)
        if not claimed:
            return Ok(False)
        order = claimed[0]
        match order.cancel_unpaid(AUTO_CANCEL_REASON, SYSTEM_ACTOR, now):
            case Err(refused):
                _log.warning(
                    "orders.overdue_cancel_refused", number=order.number, code=refused.code
                )
                return Ok(False)
            case Ok(_):
                pass
        if order.reservation_id is not None:
            match await self._stocks.release(order.reservation_id):
                case Err(refused):
                    _log.warning(
                        "orders.overdue_release_refused", number=order.number, code=refused.code
                    )
                    return Ok(False)
                case Ok(_):
                    pass
        await self._orders.save(order)
        await self._events.publish(order.pull_events())
        return Ok(True)
