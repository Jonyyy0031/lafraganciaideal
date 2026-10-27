"""Ports of the payments module, in payments' terms (adapters implement them)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.payments.contracts import PendingTicket
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.shared.kernel import DomainError, Result


@dataclass(frozen=True, slots=True)
class PayableLine:
    presentation_id: UUID
    label: str
    quantity: int
    unit_price_cents: int


@dataclass(frozen=True, slots=True)
class PayableOrder:
    """The facts of an order that a payment needs (payments' own copy)."""

    order_id: UUID
    number: str
    status: str
    payment_due_at: datetime
    customer_name: str
    customer_email: str
    lines: list[PayableLine]
    delivery_cents: int
    total_cents: int


class PayableOrders(Protocol):
    async def payable(self, access_token: str) -> PayableOrder | None:
        """The order behind the private-link token, or None."""
        ...


@dataclass(frozen=True, slots=True)
class CheckoutItem:
    """One charged item; `id` is the presentation id or `"shipping"`."""

    id: str
    title: str
    quantity: int
    unit_price_cents: int


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    order_id: UUID
    number: str
    payer_name: str
    payer_email: str
    items: list[CheckoutItem]
    expires_at: datetime
    back_url: str
    # The latest expiry of a cash or transfer ticket issued from this link (decision 5).
    cash_expires_at: datetime


@dataclass(frozen=True, slots=True)
class CheckoutLink:
    preference_id: str
    redirect_url: str


@dataclass(frozen=True, slots=True)
class GatewayPayment:
    """A payment as Mercado Pago reports it; `external_reference` is the order id we sent.
    `expires_at` (`date_of_expiration`) and `ticket_url` (the voucher or instructions page,
    `transaction_details.external_resource_url`) matter for cash and transfer tickets."""

    payment_id: str
    status: str
    status_detail: str | None
    external_reference: str | None
    amount_cents: int
    currency: str
    payment_type: str | None
    approved_at: datetime | None
    payment_method: str | None = None
    expires_at: datetime | None = None
    ticket_url: str | None = None


class PaymentGateway(Protocol):
    """The payment provider. Calls go over the network: never inside a transaction."""

    async def create_checkout(
        self, request: CheckoutRequest
    ) -> Result[CheckoutLink, PaymentGatewayUnavailable]: ...

    async def get_payment(
        self, payment_id: str
    ) -> Result[GatewayPayment | None, PaymentGatewayUnavailable]:
        """The payment's current state; None when Mercado Pago does not know it (404)."""
        ...

    async def close_checkout(
        self, preference_id: str, at: datetime
    ) -> Result[bool, PaymentGatewayUnavailable]:
        """Expire the preference at `at`, so its link takes no more payments (decision 18).
        False when Mercado Pago does not know the preference (404) or refuses the change
        for good (400, 409 or 422). Any other failure is an `Err`, so the caller retries."""
        ...

    async def cancel_payment(self, payment_id: str) -> Result[bool, PaymentGatewayUnavailable]:
        """Cancel a pending cash or transfer ticket so it can no longer be paid (decision 20).
        False when Mercado Pago refuses it for good (400, 404, 409 or 422: e.g. already paid or
        already expired). Any other failure is an `Err`, so the caller retries."""
        ...


@dataclass(frozen=True, slots=True)
class Confirmation:
    """What applying a payment did to the order (payments' own copy). `outcome` is `paid`,
    `not_pending`, `amount_mismatch`, `stock_gone` or `not_found` for `confirm`, and `held`,
    `not_pending` or `not_found` for `hold`."""

    outcome: str
    number: int | None


class OrderConfirmations(Protocol):
    async def confirm(
        self, order_id: UUID, payment_id: str, amount_cents: int, currency: str
    ) -> Result[Confirmation, DomainError]:
        """Apply an approved payment to the order. Joins the caller's transaction."""
        ...

    async def hold(
        self, order_id: UUID, until: datetime, method: str, instructions_url: str | None
    ) -> Result[Confirmation, DomainError]:
        """Keep a pending order waiting for a cash or transfer ticket until `until` (never
        earlier than its current deadline) and email the customer the instructions. Joins the
        caller's transaction."""
        ...


class PaymentQueries(Protocol):
    """Read side of the payments: returns response models directly (`Database.reader()`)."""

    async def pending_for_order(self, order_id: UUID, now: datetime) -> list[PendingTicket]:
        """The order's payments that are `pending`, of a cash type and expire after `now`,
        newest first."""
        ...


@dataclass(frozen=True, slots=True)
class OwnerContact:
    name: str
    email: str


class OwnerContacts(Protocol):
    async def active_owners(self) -> list[OwnerContact]:
        """The active owners, ordered by name."""
        ...
