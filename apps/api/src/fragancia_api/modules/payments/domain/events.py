from dataclasses import dataclass
from typing import ClassVar

from fragancia_api.shared.kernel import DomainEvent


@dataclass(frozen=True, kw_only=True)
class PaymentNotificationReceived(DomainEvent):
    """A verified Mercado Pago webhook named this payment; the worker fetches and applies it."""

    name: ClassVar[str] = "payments.notification.received"
    payment_id: str


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
