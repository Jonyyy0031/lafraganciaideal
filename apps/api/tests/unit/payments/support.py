"""Builders shared by the payments unit tests."""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.application.ports import (
    CheckoutItem,
    CheckoutRequest,
    PayableLine,
    PayableOrder,
)
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.infrastructure.in_memory import (
    FakeGateway,
    InMemoryCheckouts,
    InMemoryPayableOrders,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
BACK_URL = "https://shop.test/pedido/pago"
TOKEN = "order-link-token"
ORDER_ID = UUID(int=900)


def make_line(n: int = 1, quantity: int = 2, unit_price_cents: int = 250_000) -> PayableLine:
    return PayableLine(
        presentation_id=UUID(int=n),
        label=f"Brand Perfume {n} EDT 100 ml",
        quantity=quantity,
        unit_price_cents=unit_price_cents,
    )


def make_payable(
    *,
    status: str = "pending_payment",
    due: timedelta = timedelta(minutes=30),
    lines: tuple[PayableLine, ...] | None = None,
    delivery_cents: int = 18_000,
    total_cents: int | None = None,
) -> PayableOrder:
    the_lines = lines if lines is not None else (make_line(1), make_line(2, 1, 99_900))
    subtotal = sum(line.quantity * line.unit_price_cents for line in the_lines)
    return PayableOrder(
        order_id=ORDER_ID,
        number="LFI-1001",
        status=status,
        payment_due_at=NOW + due,
        customer_name="Ana López",
        customer_email="ana@example.com",
        lines=list(the_lines),
        delivery_cents=delivery_cents,
        total_cents=subtotal + delivery_cents if total_cents is None else total_cents,
    )


def make_checkout(expires_at: datetime, *, preference_id: str = "pref-stored") -> Checkout:
    return Checkout(
        order_id=ORDER_ID,
        preference_id=preference_id,
        redirect_url=f"https://pay.test/{preference_id}",
        expires_at=expires_at,
        created_at=NOW - timedelta(minutes=10),
    )


def make_request(**overrides: object) -> CheckoutRequest:
    values: dict[str, object] = {
        "order_id": ORDER_ID,
        "number": "LFI-1001",
        "payer_name": "Ana López",
        "payer_email": "ana@example.com",
        "items": [
            CheckoutItem("00000000-0000-0000-0000-000000000001", "Brand Perfume 1", 2, 250_000),
            CheckoutItem("shipping", "Envío", 1, 18_000),
        ],
        "expires_at": datetime(2026, 10, 22, 18, 30, tzinfo=UTC),
        "back_url": BACK_URL,
        "cash_expires_at": datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return CheckoutRequest(**values)  # type: ignore[arg-type]


@dataclass
class World:
    """`StartCheckout` over in-memory adapters; the clock is `NOW`."""

    gateway: FakeGateway = field(default_factory=FakeGateway)
    checkouts: InMemoryCheckouts = field(default_factory=InMemoryCheckouts)
    orders: InMemoryPayableOrders = field(default_factory=InMemoryPayableOrders)

    def __post_init__(self) -> None:
        self.clock = FixedClock(NOW)

    def put(self, order: PayableOrder, token: str = TOKEN) -> None:
        self.orders.by_token[token] = order

    def start_checkout(self, gateway: object | None = None) -> StartCheckout:
        return StartCheckout(
            orders=self.orders,
            checkouts=self.checkouts,
            gateway=gateway if gateway is not None else self.gateway,  # type: ignore[arg-type]
            transactions=InMemoryTransactionRunner(),
            clock=self.clock,
            back_url=BACK_URL,
        )


# --- the webhook (plan 002) --------------------------------------------------------------------

SECRET = "webhook-secret-for-tests"
TS = "1700000000"


def sign(
    data_id: str | None,
    request_id: str | None,
    *,
    secret: str = SECRET,
    ts: str = TS,
) -> str:
    """The `x-signature` header Mercado Pago would send: the SDK's manifest, HMAC-SHA256 hex.
    Built here from the documented format, not with the code under test."""
    manifest = ""
    if data_id:
        manifest += f"id:{data_id};"
    if request_id:
        manifest += f"request-id:{request_id};"
    manifest += f"ts:{ts};"
    digest = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts},v1={digest}"
