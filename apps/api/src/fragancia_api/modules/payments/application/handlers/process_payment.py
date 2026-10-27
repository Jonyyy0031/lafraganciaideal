from uuid import UUID

import structlog

from fragancia_api.modules.payments.application.ports import (
    GatewayPayment,
    OrderConfirmations,
    PaymentGateway,
)
from fragancia_api.modules.payments.domain.events import PaymentFlagged
from fragancia_api.modules.payments.domain.payment import (
    APPROVED,
    METHOD_LABELS,
    Payment,
    PaymentOutcome,
)
from fragancia_api.modules.payments.domain.repositories import PaymentRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventMessage, EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

# The facade's outcomes (`Confirmation.outcome`).
_PAID = "paid"
_NOT_PENDING = "not_pending"
_AMOUNT_MISMATCH = "amount_mismatch"
_STOCK_GONE = "stock_gone"
_HELD = "held"


class ProcessPaymentNotification:
    """`payments.notification.received`: fetch the payment from Mercado Pago and apply it
    (decisions 2, 3 and 8 to 12).

    Idempotent by payment id: every delivery stores the payment's current state, and an
    approved payment is applied to its order only while its `outcome` is unset, so a duplicate
    or replayed notification changes nothing. A pending cash or transfer ticket holds its order
    until the ticket expires, once per payment (`held_at`), so the instructions email is sent
    once. Mercado Pago down, or a refusal from orders, raises: the relay rolls the whole
    transaction back and retries the event.
    """

    def __init__(
        self,
        *,
        gateway: PaymentGateway,
        payments: PaymentRepository,
        orders: OrderConfirmations,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._gateway = gateway
        self._payments = payments
        self._orders = orders
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def __call__(self, message: EventMessage) -> None:
        payment_id = str(message.payload["payment_id"])
        match await self._gateway.get_payment(payment_id):
            case Err(_):
                raise RuntimeError("payments.gateway_unavailable")
            case Ok(found):
                fetched = found
        if fetched is None:
            # E.g. the test notification of Mercado Pago's panel.
            _log.warning("payments.payment_not_found", payment_id=payment_id)
            return
        try:
            order_id = UUID(fetched.external_reference or "")
        except ValueError:
            _log.warning("payments.payment_unmatched", payment_id=payment_id)
            return

        async def work() -> Result[None, DomainError]:
            payment = await self._stored(fetched, order_id)
            if payment.status == APPROVED and payment.outcome is None:
                match await self._apply(payment, order_id):
                    case Err(error):
                        return Err(error)
                    case Ok(_):
                        pass
            if payment.needs_hold():
                match await self._hold(payment, order_id):
                    case Err(error):
                        return Err(error)
                    case Ok(_):
                        pass
            await self._payments.save(payment)
            return Ok(None)

        match await self._transactions.run(work):
            case Err(error):
                raise RuntimeError(f"payments.confirm_refused: {error.code}")
            case Ok(_):
                pass

    async def _stored(self, fetched: GatewayPayment, order_id: UUID) -> Payment:
        now = self._clock.now()
        # `claim` takes the row lock before anything is read, even for a payment seen for the
        # first time, so a concurrent delivery of the same payment waits here and then sees the
        # `outcome` this one stored (plan 002 review H1).
        payment = await self._payments.claim(
            Payment(
                payment_id=fetched.payment_id,
                order_id=order_id,
                status=fetched.status,
                status_detail=fetched.status_detail,
                amount_cents=fetched.amount_cents,
                currency=fetched.currency,
                payment_type=fetched.payment_type,
                approved_at=fetched.approved_at,
                outcome=None,
                created_at=now,
                updated_at=now,
                payment_method=fetched.payment_method,
                expires_at=fetched.expires_at,
                ticket_url=fetched.ticket_url,
            )
        )
        payment.refresh(fetched.status, fetched.status_detail, fetched.approved_at, now)
        payment.describe(fetched.payment_method, fetched.expires_at, fetched.ticket_url)
        return payment

    async def _hold(self, payment: Payment, order_id: UUID) -> Result[None, DomainError]:
        """Keep the order waiting for a pending cash or transfer ticket (decision 5). Once per
        payment: `held_at` is set whatever orders answers, except a refusal (retried)."""
        if payment.expires_at is None or payment.payment_type is None:
            raise ValueError("only a pending ticket with an expiry is held")
        held = await self._orders.hold(
            order_id,
            until=payment.expires_at,
            method=METHOD_LABELS[payment.payment_type],
            instructions_url=payment.ticket_url,
        )
        match held:
            case Err(error):
                return Err(error)
            case Ok(confirmation):
                pass
        if confirmation.outcome != _HELD:
            # `not_pending` or `not_found`: a ticket for a paid or cancelled order (or for no
            # order) needs nothing more, and is never retried.
            _log.info(
                "payments.ticket_not_held",
                payment_id=payment.payment_id,
                outcome=confirmation.outcome,
            )
        payment.mark_held(self._clock.now())
        return Ok(None)

    async def _apply(self, payment: Payment, order_id: UUID) -> Result[None, DomainError]:
        confirmed = await self._orders.confirm(
            order_id, payment.payment_id, payment.amount_cents, payment.currency
        )
        match confirmed:
            case Err(error):
                return Err(error)
            case Ok(confirmation):
                pass
        now = self._clock.now()
        outcome = confirmation.outcome
        if outcome == _PAID:
            payment.settle(PaymentOutcome.APPLIED, now)
        elif outcome == _NOT_PENDING:
            payment.settle(PaymentOutcome.REFUND_DUE, now)
        elif outcome == _STOCK_GONE:
            payment.settle(PaymentOutcome.REFUND_DUE, now)
            await self._flag(payment, confirmation.number, _STOCK_GONE)
        elif outcome == _AMOUNT_MISMATCH:
            payment.settle(PaymentOutcome.AMOUNT_MISMATCH, now)
            await self._flag(payment, confirmation.number, _AMOUNT_MISMATCH)
        else:
            # `not_found`: the order id matches no order; the outcome stays unset.
            _log.warning(
                "payments.payment_unmatched", payment_id=payment.payment_id, outcome=outcome
            )
        return Ok(None)

    async def _flag(self, payment: Payment, number: int | None, reason: str) -> None:
        if number is None:
            raise ValueError("a flagged payment needs the order number")
        await self._events.publish(
            [
                PaymentFlagged(
                    payment_id=payment.payment_id,
                    number=number,
                    reason=reason,
                    amount_cents=payment.amount_cents,
                    currency=payment.currency,
                )
            ]
        )
