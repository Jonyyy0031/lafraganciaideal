from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

APPROVED = "approved"


class PaymentOutcome(StrEnum):
    """What an approved payment did. Set once; plan 004 refunds `refund_due`."""

    APPLIED = "applied"
    REFUND_DUE = "refund_due"
    AMOUNT_MISMATCH = "amount_mismatch"


def amount_to_cents(value: Decimal | float | int) -> int:
    """Mercado Pago's amount in pesos to integer cents, rounded half-up."""
    cents = (Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)


@dataclass(slots=True)
class Payment:
    """A Mercado Pago payment as last fetched (one row per payment id). `order_id` is the
    payment's `external_reference`; `outcome` stays None until an approved payment is applied
    to its order, and is never set twice."""

    payment_id: str
    order_id: UUID | None
    status: str
    status_detail: str | None
    amount_cents: int
    currency: str
    payment_type: str | None
    approved_at: datetime | None
    outcome: PaymentOutcome | None
    created_at: datetime
    updated_at: datetime

    def refresh(
        self, status: str, status_detail: str | None, approved_at: datetime | None, now: datetime
    ) -> None:
        """Take the fields Mercado Pago reports now."""
        self.status = status
        self.status_detail = status_detail
        self.approved_at = approved_at
        self.updated_at = now

    def settle(self, outcome: PaymentOutcome, now: datetime) -> None:
        if self.outcome is not None:
            raise ValueError("the payment already has an outcome")
        self.outcome = outcome
        self.updated_at = now
