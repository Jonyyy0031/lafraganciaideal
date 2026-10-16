"""HTTP contracts of orders (source of truth for OpenAPI and the generated web client)."""

from typing import Literal
from uuid import UUID

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


class CartLineView(BaseModel):
    """A cart line as the buyer sees it now. `status` is `ok`, `unavailable` (unknown, archived
    or hidden: the catalog fields are `null`), `sold_out` or `insufficient_stock`. Only `ok`
    lines count in the cart's totals. `price_changed_from_cents` is the price the buyer last
    acknowledged when it differs from `unit_price_cents`."""

    presentation_id: UUID
    quantity: int
    status: Literal["ok", "unavailable", "sold_out", "insufficient_stock"]
    label: str | None
    perfume_slug: str | None
    card_url: str | None
    ml: int | None
    availability: Literal["in_stock", "made_to_order"] | None
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    available_units: int | None
    unit_price_cents: int | None
    line_total_cents: int | None
    price_changed_from_cents: int | None


class CartView(BaseModel):
    """The cart with current prices and availability. It never reserves stock.
    `lead_time_max_days` is the longest lead time over the `ok` made-to-order lines."""

    lines: list[CartLineView]
    subtotal_cents: int
    item_count: int
    has_problems: bool
    lead_time_max_days: int | None


class SetCartLineRequest(BaseModel):
    """The quantity bounds (1 to 10) are a domain rule; the payload is only kept sane."""

    quantity: int = Field(ge=-_CENTS_BOUND, le=_CENTS_BOUND, examples=[2])


class DeliveryQuoteLine(BaseModel):
    method: Literal["home", "local", "pickup"]
    cost_cents: int = Field(ge=0)


class DeliveryQuoteResponse(BaseModel):
    """The methods that apply, in the order home, local, pickup; empty when none does."""

    options: list[DeliveryQuoteLine]
