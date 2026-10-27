from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from fragancia_api.shared.kernel import DomainEvent


@dataclass(frozen=True, kw_only=True)
class PaymentNotificationReceived(DomainEvent):
    """A verified Mercado Pago webhook named this payment; the worker fetches and applies it."""

    name: ClassVar[str] = "payments.notification.received"
    payment_id: str


@dataclass(frozen=True, kw_only=True)
class CheckoutCloseRequested(DomainEvent):
    """The order left `pending_payment` (paid or cancelled): its Mercado Pago link must stop
    taking payments (decision 18). Its own outbox row, so a Mercado Pago outage retries only
    the close, never orders' emails."""

    name: ClassVar[str] = "payments.checkout.close_requested"
    order_id: UUID


@dataclass(frozen=True, kw_only=True)
class PaymentFlagged(DomainEvent):
    """An approved payment the owners must review: `reason` is `amount_mismatch` (the order
    stays pending) or `stock_gone` (the order was cancelled)."""

    name: ClassVar[str] = "payments.payment.flagged"
    payment_id: str
    number: int
    reason: str
    amount_cents: int
    currency: str
