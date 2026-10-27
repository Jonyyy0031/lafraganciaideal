"""HTTP contracts of payments (source of truth for OpenAPI and the generated web client)."""

from datetime import datetime

from pydantic import BaseModel, Field


class StartCheckoutRequest(BaseModel):
    """The order's private-link token, in the body so it never travels in a URL."""

    access_token: str = Field(max_length=200)


class StartCheckoutResponse(BaseModel):
    """Where to send the guest to pay: the Mercado Pago checkout."""

    redirect_url: str


class PendingTicketsRequest(BaseModel):
    """The order's private-link token, in the body so it never travels in a URL."""

    access_token: str = Field(max_length=200)


class PendingTicket(BaseModel):
    """A cash or transfer ticket issued for the order and not paid yet (decision 16)."""

    method: str = Field(description='How it is paid: "efectivo en tienda" or "transferencia SPEI".')
    amount_cents: int
    expires_at: datetime = Field(
        description=(
            "When the ticket stops paying the order: its own expiry or the order's payment"
            " deadline, whichever is earlier."
        )
    )
    ticket_url: str | None = Field(description="Mercado Pago's page with the instructions.")


class PendingTickets(BaseModel):
    """The order's live tickets, newest first; empty when the order is not waiting for
    payment."""

    items: list[PendingTicket]


class WebhookAck(BaseModel):
    """The webhook was accepted (or ignored on purpose): Mercado Pago must not retry it."""

    received: bool = True
