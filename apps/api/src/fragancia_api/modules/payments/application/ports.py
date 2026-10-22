"""Ports of the payments module, in payments' terms (adapters implement them)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.shared.kernel import Result


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


@dataclass(frozen=True, slots=True)
class CheckoutLink:
    preference_id: str
    redirect_url: str


class PaymentGateway(Protocol):
    """The payment provider. Calls go over the network: never inside a transaction."""

    async def create_checkout(
        self, request: CheckoutRequest
    ) -> Result[CheckoutLink, PaymentGatewayUnavailable]: ...
