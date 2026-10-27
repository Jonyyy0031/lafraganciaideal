"""The facade payments uses to apply an approved Mercado Pago payment to an order."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import structlog

from fragancia_api.modules.orders.application.commands.lifecycle import OrderMove
from fragancia_api.modules.orders.domain.order import PAYMENTS_ACTOR, SYSTEM_ACTOR, OrderStatus
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

PAID_NOTE = "Pagado con Mercado Pago (pago {payment_id})"
STOCK_GONE_REASON = "Pago recibido sin existencias (pago {payment_id})"
CURRENCY = "MXN"
# Inventory's refusals of a commit whose units can no longer be sold.
_STOCK_GONE_CODES = frozenset({"INVENTORY_STOCK_UNAVAILABLE", "INVENTORY_STOCK_INSUFFICIENT"})


class PaymentConfirmationOutcome(StrEnum):
    PAID = "paid"
    NOT_PENDING = "not_pending"
    AMOUNT_MISMATCH = "amount_mismatch"
    STOCK_GONE = "stock_gone"
    NOT_FOUND = "not_found"
    # `hold_for_payment`: the order now waits for the ticket.
    HELD = "held"


@dataclass(frozen=True, slots=True)
class PaymentConfirmation:
    """What a payment did to the order. `number` is None only for `not_found`; `status` is the
    order's status for `not_pending`."""

    outcome: PaymentConfirmationOutcome
    number: int | None = None
    status: str | None = None


class OrderPaymentConfirmations(OrderMove):
    """Facade: the only way other modules (payments) apply an approved payment to an order.

    `confirm` moves a `pending_payment` order whose total and currency match to `paid`
    (committing its reservation, even past the deadline while the units can be sold), or cancels
    it when inventory refuses the commit because the units are gone. The commit and the move
    happen in one transaction, so a committed reservation never sits on a pending order
    (initiative README, "Dependency notes"). `hold_for_payment` keeps a pending order waiting
    for a cash or transfer ticket. Both join the caller's transaction.
    """

    async def confirm(
        self, order_id: UUID, payment_id: str, amount_cents: int, currency: str
    ) -> Result[PaymentConfirmation, DomainError]:
        async def work() -> Result[PaymentConfirmation, DomainError]:
            order = await self._orders.get_by_id_for_update(order_id)
            if order is None:
                return Ok(PaymentConfirmation(PaymentConfirmationOutcome.NOT_FOUND))
            if order.status is not OrderStatus.PENDING_PAYMENT:
                return Ok(
                    PaymentConfirmation(
                        PaymentConfirmationOutcome.NOT_PENDING, order.number, order.status.value
                    )
                )
            if currency != CURRENCY or amount_cents != order.total_cents:
                return Ok(
                    PaymentConfirmation(PaymentConfirmationOutcome.AMOUNT_MISMATCH, order.number)
                )
            now = self._clock.now()
            if order.reservation_id is not None:
                match await self._stocks.commit(order.reservation_id):
                    case Err(refused) if refused.code in _STOCK_GONE_CODES:
                        match order.cancel_unpaid(
                            STOCK_GONE_REASON.format(payment_id=payment_id), PAYMENTS_ACTOR, now
                        ):
                            case Err(error):
                                return Err(error)
                            case Ok(_):
                                pass
                        match await self._stocks.release(order.reservation_id):
                            case Err(error):
                                return Err(error)
                            case Ok(_):
                                pass
                        await self._orders.save(order)
                        await self._events.publish(order.pull_events())
                        return Ok(
                            PaymentConfirmation(PaymentConfirmationOutcome.STOCK_GONE, order.number)
                        )
                    case Err(error):
                        return Err(error)
                    case Ok(_):
                        pass
            match order.mark_paid(PAID_NOTE.format(payment_id=payment_id), SYSTEM_ACTOR, now):
                case Err(error):
                    return Err(error)
                case Ok(_):
                    pass
            await self._orders.save(order)
            await self._events.publish(order.pull_events())
            return Ok(PaymentConfirmation(PaymentConfirmationOutcome.PAID, order.number))

        return await self._transactions.run(work)

    async def hold_for_payment(
        self, order_id: UUID, until: datetime, method: str, instructions_url: str | None
    ) -> Result[PaymentConfirmation, DomainError]:
        """A cash or transfer ticket was issued (payments decisions 5 and 15): a
        `pending_payment` order's deadline moves to `until` when that is later, and its
        reservation is extended to it, in one transaction. A refused extension (the
        reservation expired or was released) is logged and keeps the new deadline: the ticket
        can still be paid, as a late payment. Records `orders.order.payment_pending`, which
        sends the customer the instructions email. Joins the caller's transaction."""

        async def work() -> Result[PaymentConfirmation, DomainError]:
            order = await self._orders.get_by_id_for_update(order_id)
            if order is None:
                return Ok(PaymentConfirmation(PaymentConfirmationOutcome.NOT_FOUND))
            if order.status is not OrderStatus.PENDING_PAYMENT:
                return Ok(
                    PaymentConfirmation(
                        PaymentConfirmationOutcome.NOT_PENDING, order.number, order.status.value
                    )
                )
            match order.await_payment(until, method, instructions_url, self._clock.now()):
                case Err(error):
                    return Err(error)
                case Ok(moved):
                    pass
            if moved and order.reservation_id is not None:
                match await self._stocks.extend(order.reservation_id, order.payment_due_at):
                    case Err(refused):
                        _log.warning(
                            "orders.payment_hold_not_extended",
                            number=order.number,
                            reservation_id=str(order.reservation_id),
                            code=refused.code,
                        )
                    case Ok(_):
                        pass
            await self._orders.save(order)
            await self._events.publish(order.pull_events())
            return Ok(PaymentConfirmation(PaymentConfirmationOutcome.HELD, order.number))

        return await self._transactions.run(work)
