"""The facade payments uses to apply an approved Mercado Pago payment to an order."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fragancia_api.modules.orders.application.commands.lifecycle import OrderMove
from fragancia_api.modules.orders.domain.order import PAYMENTS_ACTOR, SYSTEM_ACTOR, OrderStatus
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

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


@dataclass(frozen=True, slots=True)
class PaymentConfirmation:
    """What an approved payment did to the order. `number` is None only for `not_found`;
    `status` is the order's status for `not_pending`."""

    outcome: PaymentConfirmationOutcome
    number: int | None = None
    status: str | None = None


class OrderPaymentConfirmations(OrderMove):
    """Facade: the only way other modules (payments) apply an approved payment to an order.

    `confirm` moves a `pending_payment` order whose total and currency match to `paid`
    (committing its reservation, even past the deadline while the units can be sold), or cancels
    it when inventory refuses the commit because the units are gone. The commit and the move
    happen in one transaction, so a committed reservation never sits on a pending order
    (initiative README, "Dependency notes"). It joins the caller's transaction.
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
