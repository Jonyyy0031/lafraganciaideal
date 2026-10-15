"""HTTP contracts of orders (source of truth for OpenAPI and the generated web client)."""

from typing import Literal

from pydantic import BaseModel, Field

# Business rules (price and postal-code ranges, required fields) live in the domain; these
# models only bound the payload.
_CENTS_BOUND = 1_000_000_000


class HomeDeliveryOptions(BaseModel):
    """Home delivery: a flat rate, free from `free_from_cents` of subtotal when that is set."""

    enabled: bool
    price_cents: int = Field(ge=-_CENTS_BOUND, le=_CENTS_BOUND, examples=[18000])
    free_from_cents: int | None = Field(
        default=None, ge=-_CENTS_BOUND, le=_CENTS_BOUND, examples=[250000]
    )


class LocalDeliveryOptions(BaseModel):
    """Local delivery: one price for a list of 5-digit postal codes."""

    enabled: bool
    price_cents: int = Field(ge=-_CENTS_BOUND, le=_CENTS_BOUND, examples=[9000])
    postal_codes: list[str] = Field(max_length=1000, examples=[["77500", "77505"]])


class PickupOptions(BaseModel):
    """Pickup at one point. Always free."""

    enabled: bool
    address: str = Field(max_length=1000)
    hours: str = Field(max_length=1000)


class DeliveryOptions(BaseModel):
    """The three delivery methods: the admin request and response body."""

    home: HomeDeliveryOptions
    local: LocalDeliveryOptions
    pickup: PickupOptions


class PublicHomeDelivery(BaseModel):
    price_cents: int = Field(ge=0)
    free_from_cents: int | None = Field(default=None, ge=1)


class PublicLocalDelivery(BaseModel):
    price_cents: int = Field(ge=0)
    postal_codes: list[str]


class PublicPickup(BaseModel):
    address: str
    hours: str


class PublicDeliveryOptions(BaseModel):
    """The enabled methods only; a disabled one is `null`."""

    home: PublicHomeDelivery | None
    local: PublicLocalDelivery | None
    pickup: PublicPickup | None


class DeliveryQuoteLine(BaseModel):
    method: Literal["home", "local", "pickup"]
    cost_cents: int = Field(ge=0)


class DeliveryQuoteResponse(BaseModel):
    """The methods that apply, in the order home, local, pickup; empty when none does."""

    options: list[DeliveryQuoteLine]
