"""HTTP contracts of payments (source of truth for OpenAPI and the generated web client)."""

from pydantic import BaseModel, Field


class StartCheckoutRequest(BaseModel):
    """The order's private-link token, in the body so it never travels in a URL."""

    access_token: str = Field(max_length=200)


class StartCheckoutResponse(BaseModel):
    """Where to send the guest to pay: the Mercado Pago checkout."""

    redirect_url: str


class WebhookAck(BaseModel):
    """The webhook was accepted (or ignored on purpose): Mercado Pago must not retry it."""

    received: bool = True
