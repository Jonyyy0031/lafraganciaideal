from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

APPROVED = "approved"
PENDING = "pending"
# The payment types that are a ticket the customer pays later: cash in stores and SPEI
# (decision 14).
CASH_TYPES = ("ticket", "bank_transfer")
# How the instructions email names each cash type (decision 15).
METHOD_LABELS = {"ticket": "efectivo en tienda", "bank_transfer": "transferencia SPEI"}


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
    to its order, and is never set twice.

    For a cash or transfer ticket, `expires_at` is its `date_of_expiration`, `ticket_url` the
    voucher or instructions page, and `held_at` when the order was held for it (once per
    payment, so the instructions email is sent once)."""

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
    payment_method: str | None = None
    expires_at: datetime | None = None
    ticket_url: str | None = None
    held_at: datetime | None = None

    def refresh(
        self, status: str, status_detail: str | None, approved_at: datetime | None, now: datetime
    ) -> None:
        """Take the fields Mercado Pago reports now."""
        self.status = status
        self.status_detail = status_detail
        self.approved_at = approved_at
        self.updated_at = now

    def describe(
        self, payment_method: str | None, expires_at: datetime | None, ticket_url: str | None
    ) -> None:
        """Take the method and the ticket's expiry and page that Mercado Pago reports now."""
        self.payment_method = payment_method
        self.expires_at = expires_at
        self.ticket_url = ticket_url

    def is_ticket(self) -> bool:
        return self.payment_type in CASH_TYPES

    def is_live_ticket(self, now: datetime) -> bool:
        """A pending cash or transfer ticket that has not expired (decision 16)."""
        return (
            self.status == PENDING
            and self.is_ticket()
            and self.expires_at is not None
            and self.expires_at > now
        )

    def needs_hold(self, now: datetime) -> bool:
        """A pending ticket, still payable at `now`, whose order was not held for it yet
        (decision 5). An already expired ticket needs none (plan 003 review L1)."""
        return (
            self.status == PENDING
            and self.is_ticket()
            and self.expires_at is not None
            and self.expires_at > now
            and self.held_at is None
        )

    def expired_on_arrival(self, now: datetime) -> bool:
        """A pending ticket not held yet that had already expired at `now`. It is marked held
        with no hold and no email (plan 003 review L1)."""
        return (
            self.status == PENDING
            and self.is_ticket()
            and self.expires_at is not None
            and self.expires_at <= now
            and self.held_at is None
        )

    def mark_held(self, now: datetime) -> None:
        self.held_at = now
        self.updated_at = now

    def settle(self, outcome: PaymentOutcome, now: datetime) -> None:
        if self.outcome is not None:
            raise ValueError("the payment already has an outcome")
        self.outcome = outcome
        self.updated_at = now
