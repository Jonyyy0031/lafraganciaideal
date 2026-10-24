"""In-memory adapters for unit tests. They honor the same contracts as the real ones."""

from dataclasses import replace
from uuid import UUID

from fragancia_api.modules.payments.application.ports import (
    CheckoutLink,
    CheckoutRequest,
    GatewayPayment,
    PayableOrder,
)
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.modules.payments.domain.payment import Payment
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryCheckouts:
    def __init__(self, *checkouts: Checkout) -> None:
        self.by_order: dict[UUID, Checkout] = {c.order_id: c for c in checkouts}

    async def get_for_order(self, order_id: UUID) -> Checkout | None:
        return self.by_order.get(order_id)

    async def upsert(self, checkout: Checkout) -> None:
        self.by_order[checkout.order_id] = checkout


class InMemoryPayments:
    """The `PaymentRepository` over a dict; it stores copies, like a database would."""

    def __init__(self, *payments: Payment) -> None:
        self.by_id: dict[str, Payment] = {p.payment_id: replace(p) for p in payments}

    async def get_for_update(self, payment_id: str) -> Payment | None:
        found = self.by_id.get(payment_id)
        return replace(found) if found is not None else None

    async def save(self, payment: Payment) -> None:
        self.by_id[payment.payment_id] = replace(payment)


class FakeGateway:
    """Records the requests; answers numbered preferences, or fails when told to. `get_payment`
    answers what `payments` holds (None when unknown), or fails when `fail` is set."""

    def __init__(
        self, *, fail: bool = False, payments: dict[str, GatewayPayment] | None = None
    ) -> None:
        self.fail = fail
        self.requests: list[CheckoutRequest] = []
        self.payments: dict[str, GatewayPayment] = dict(payments or {})
        self.fetched: list[str] = []

    async def get_payment(
        self, payment_id: str
    ) -> Result[GatewayPayment | None, PaymentGatewayUnavailable]:
        self.fetched.append(payment_id)
        if self.fail:
            return Err(PaymentGatewayUnavailable())
        return Ok(self.payments.get(payment_id))

    async def create_checkout(
        self, request: CheckoutRequest
    ) -> Result[CheckoutLink, PaymentGatewayUnavailable]:
        self.requests.append(request)
        if self.fail:
            return Err(PaymentGatewayUnavailable())
        number = len(self.requests)
        return Ok(
            CheckoutLink(
                preference_id=f"pref-{number}",
                redirect_url=f"https://pay.test/checkout/pref-{number}",
            )
        )


class InMemoryPayableOrders:
    """Orders by access token."""

    def __init__(self, **orders: PayableOrder) -> None:
        self.by_token: dict[str, PayableOrder] = dict(orders)

    async def payable(self, access_token: str) -> PayableOrder | None:
        return self.by_token.get(access_token)
