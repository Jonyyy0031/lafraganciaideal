"""HTTP contracts of orders (source of truth for OpenAPI and the generated web client)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from fragancia_api.shared.contracts import Page

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
    """A cart line as the buyer sees it now. `status` is `ok`, `unavailable` (the presentation is
    hidden or archived, which keeps its catalog fields and price, or no longer exists, which
    leaves them `null`), `sold_out` or `insufficient_stock`. Only `ok`
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


class CheckoutContact(BaseModel):
    """Who is buying (decisions 1 and 29). The phone is 10 digits once spaces, dashes and a
    leading +52 are ignored."""

    name: str = Field(max_length=500, examples=["Ana López"])
    email: str = Field(max_length=500, examples=["ana@example.com"])
    phone: str = Field(max_length=500, examples=["+52 998 123 4567"])


class CheckoutAddress(BaseModel):
    """A Mexican address (decision 28). `state` is the ISO 3166-2:MX code, e.g. `CMX`."""

    street: str = Field(max_length=500)
    exterior_number: str = Field(max_length=500)
    interior_number: str | None = Field(default=None, max_length=500)
    neighborhood: str = Field(max_length=500)
    postal_code: str = Field(max_length=500, examples=["01000"])
    municipality: str = Field(max_length=500)
    state: str = Field(max_length=500, examples=["CMX"])
    references: str | None = Field(default=None, max_length=1000)


class CheckoutInvoice(BaseModel):
    """Invoice (CFDI) data, checked against the SAT catalogs (decision 27)."""

    rfc: str = Field(max_length=500, examples=["XAXX010101000"])
    business_name: str = Field(max_length=500)
    tax_regime: str = Field(max_length=500, examples=["601"])
    cfdi_use: str = Field(max_length=500, examples=["G03"])
    postal_code: str = Field(max_length=500, examples=["01000"])


class CheckoutRequest(BaseModel):
    """`address` is required for `home` and `local`, and must be absent for `pickup`.
    `expected_total_cents` is the total the customer saw; a different real total refuses the
    checkout (decision 26)."""

    contact: CheckoutContact
    delivery_method: Literal["home", "local", "pickup"]
    address: CheckoutAddress | None = None
    invoice: CheckoutInvoice | None = None
    expected_total_cents: int = Field(ge=-_CENTS_BOUND, le=_CENTS_BOUND)


class PlacedOrder(BaseModel):
    """The new order. `access_token` is the private-link token (decision 13), shown only here.
    The order waits for payment until `payment_due_at` (decisions 18 and 24)."""

    number: str = Field(examples=["LFI-1001"])
    access_token: str
    status: Literal["pending_payment"]
    subtotal_cents: int
    delivery_cents: int
    total_cents: int
    payment_due_at: datetime
    lead_time_max_days: int | None


class DeliveryQuoteLine(BaseModel):
    method: Literal["home", "local", "pickup"]
    cost_cents: int = Field(ge=0)


class DeliveryQuoteResponse(BaseModel):
    """The methods that apply, in the order home, local, pickup; empty when none does."""

    options: list[DeliveryQuoteLine]


OrderStatusName = Literal[
    "pending_payment", "paid", "preparing", "shipped", "ready_for_pickup", "delivered", "cancelled"
]
RefundStatusName = Literal["pending", "done"]


class AdminOrderSummary(BaseModel):
    """One row of the panel's order list."""

    number: str = Field(examples=["LFI-1001"])
    created_at: datetime
    customer_name: str
    customer_email: str
    status: OrderStatusName
    delivery_method: Literal["home", "local", "pickup"]
    total_cents: int
    refund_status: RefundStatusName | None


class AdminOrderPage(Page[AdminOrderSummary]): ...


class AdminOrderLine(BaseModel):
    """The frozen copy of the offer at checkout (decision 11)."""

    presentation_id: UUID
    perfume_id: UUID
    label: str
    ml: int
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    quantity: int
    unit_price_cents: int
    regular_price_cents: int
    line_total_cents: int


class AdminOrderAddress(BaseModel):
    street: str
    exterior_number: str
    interior_number: str | None
    neighborhood: str
    postal_code: str
    municipality: str
    state: str
    references: str | None


class AdminOrderInvoice(BaseModel):
    rfc: str
    business_name: str
    tax_regime: str
    cfdi_use: str
    postal_code: str


class AdminOrderEvent(BaseModel):
    """One history row. `actor` is an admin user id, `system`, `customer` or `payments` (a
    cancel because a late payment found no stock); a refund mark has the same
    status in `from_status` and `to_status`."""

    at: datetime
    from_status: OrderStatusName
    to_status: OrderStatusName
    actor: str
    note: str | None
    carrier: str | None
    tracking_number: str | None


class AdminOrderNote(BaseModel):
    """An internal note; `actor` is the admin user id."""

    at: datetime
    actor: str
    text: str


class AdminOrderDetail(BaseModel):
    """An order with everything the panel shows. `history` is oldest first, `notes` newest
    first. `reservation_id` is `null` for an order made only of made-to-order lines."""

    number: str = Field(examples=["LFI-1001"])
    status: OrderStatusName
    created_at: datetime
    updated_at: datetime
    payment_due_at: datetime
    customer_name: str
    customer_email: str
    customer_phone: str
    delivery_method: Literal["home", "local", "pickup"]
    delivery_cents: int
    address: AdminOrderAddress | None
    pickup_address: str | None
    pickup_hours: str | None
    invoice: AdminOrderInvoice | None
    lines: list[AdminOrderLine]
    subtotal_cents: int
    total_cents: int
    lead_time_max_days: int | None
    cancel_reason: str | None
    refund_status: RefundStatusName | None
    shipping_carrier: str | None
    tracking_number: str | None
    reservation_id: UUID | None
    history: list[AdminOrderEvent]
    notes: list[AdminOrderNote]


class CustomerOrderLine(BaseModel):
    label: str
    ml: int
    quantity: int
    unit_price_cents: int
    line_total_cents: int
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None
    lead_time_max_days: int | None


class CustomerOrderStep(BaseModel):
    """The order reached `status` at `at`."""

    status: OrderStatusName
    at: datetime


class CustomerOrderView(BaseModel):
    """The customer's page of an order (decisions 13, 40 and 41). No internal notes, no actor
    ids, no cancel reason, no reservation id, no email or token digests. `timeline` is oldest
    first and starts with `pending_payment` at `created_at`; the refund mark is not an entry
    (`refund_status` says it). `cancelled_by` says who cancelled, not why."""

    number: str = Field(examples=["LFI-1001"])
    status: OrderStatusName
    created_at: datetime
    payment_due_at: datetime
    customer_name: str
    customer_phone: str
    lines: list[CustomerOrderLine]
    subtotal_cents: int
    delivery_cents: int
    total_cents: int
    lead_time_max_days: int | None
    delivery_method: Literal["home", "local", "pickup"]
    address: AdminOrderAddress | None
    pickup_address: str | None
    pickup_hours: str | None
    invoice: AdminOrderInvoice | None
    shipping_carrier: str | None
    tracking_number: str | None
    refund_status: RefundStatusName | None
    cancelled_by: Literal["customer", "system", "shop"] | None
    timeline: list[CustomerOrderStep]
    can_cancel: bool


class OrderAccessRequest(BaseModel):
    """The private-link token, in the body so it never travels in a URL path (decision 42)."""

    access_token: str = Field(max_length=200)


class OrderLookupRequest(BaseModel):
    number: str = Field(max_length=100, examples=["LFI-1001"])
    email: str = Field(max_length=300)


class OrderLookupResponse(BaseModel):
    """The private-link token: a secret that opens the order."""

    access_token: str


# The rules (required, 1 to 300 characters, who may ship what) live in the domain; these models
# only bound the payload.
class MarkPaidRequest(BaseModel):
    """How the payment was received, e.g. "Transferencia directa" (decision 32)."""

    note: str = Field(max_length=1000, examples=["Transferencia directa"])


class AdvanceOrderRequest(BaseModel):
    """`carrier` and `tracking_number` are required when shipping a home delivery and must be
    absent otherwise (decision 33)."""

    to: Literal["preparing", "shipped", "ready_for_pickup", "delivered"]
    carrier: str | None = Field(default=None, max_length=1000, examples=["Estafeta"])
    tracking_number: str | None = Field(default=None, max_length=1000, examples=["123456"])


class CancelOrderRequest(BaseModel):
    reason: str = Field(max_length=1000, examples=["El cliente lo pidió"])


class RefundDoneRequest(BaseModel):
    note: str = Field(max_length=1000, examples=["Reembolso por transferencia"])


class OrderNoteRequest(BaseModel):
    text: str = Field(max_length=5000)
