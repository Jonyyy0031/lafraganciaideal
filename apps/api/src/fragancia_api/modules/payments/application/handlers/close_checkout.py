"""Close the Mercado Pago link of an order that stopped waiting for payment (decision 18).

Two subscribers, so a Mercado Pago outage retries only the close: `RequestCheckoutClose`
listens to orders' `orders.order.status_changed` and only publishes
`payments.checkout.close_requested` (no network call, it cannot fail for Mercado Pago), and
`CloseCheckout` handles that event in its own outbox row.
"""

from uuid import UUID

import structlog

from fragancia_api.modules.payments.application.ports import PaymentGateway
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.domain.events import CheckoutCloseRequested
from fragancia_api.modules.payments.domain.repositories import CheckoutRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventMessage, EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

PENDING_PAYMENT = "pending_payment"
# The moves after which an order no longer accepts payments from its old link.
_CLOSING_STATUSES = frozenset({"paid", "cancelled"})


class RequestCheckoutClose:
    """`orders.order.status_changed`: when an order moves from `pending_payment` to `paid` or
    `cancelled`, publish `payments.checkout.close_requested` in the relay's transaction."""

    def __init__(self, *, events: EventPublisher, transactions: TransactionRunner) -> None:
        self._events = events
        self._transactions = transactions

    async def __call__(self, message: EventMessage) -> None:
        if str(message.payload["from_status"]) != PENDING_PAYMENT:
            return
        if str(message.payload["to_status"]) not in _CLOSING_STATUSES:
            return
        order_id = UUID(str(message.payload["order_id"]))

        async def publish() -> Result[None, DomainError]:
            await self._events.publish([CheckoutCloseRequested(order_id=order_id)])
            return Ok(None)

        await self._transactions.run(publish)


class CloseCheckout:
    """`payments.checkout.close_requested`: expire the order's Mercado Pago preference so the
    old link takes no more payments or tickets, and close the stored checkout so it is never
    reused (decision 18).

    Idempotent: closing an already closed preference again changes nothing. Mercado Pago down
    raises, so the relay retries this event alone. A guest already in the checkout can still
    finish a payment; it is recorded as any other (plan 002), with no hold.
    """

    def __init__(
        self,
        *,
        gateway: PaymentGateway,
        checkouts: CheckoutRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._gateway = gateway
        self._checkouts = checkouts
        self._transactions = transactions
        self._clock = clock

    async def __call__(self, message: EventMessage) -> None:
        order_id = UUID(str(message.payload["order_id"]))

        async def read() -> Result[Checkout | None, DomainError]:
            return Ok(await self._checkouts.get_for_order(order_id))

        match await self._transactions.run(read):
            case Err(error):
                raise RuntimeError(f"payments.checkout_read_failed: {error.code}")
            case Ok(found):
                checkout = found
        if checkout is None:
            return
        now = self._clock.now()
        match await self._gateway.close_checkout(checkout.preference_id, now):
            case Err(_):
                raise RuntimeError("payments.gateway_unavailable")
            case Ok(known):
                if not known:
                    _log.warning(
                        "payments.checkout_missing",
                        order_id=str(order_id),
                        preference_id=checkout.preference_id,
                    )

        async def close() -> Result[None, DomainError]:
            await self._checkouts.close(order_id, now)
            return Ok(None)

        await self._transactions.run(close)
