"""In-memory adapters for unit tests. They honor the same contracts as the real ones."""

from uuid import UUID

from fragancia_api.modules.payments.application.ports import (
    CheckoutLink,
    CheckoutRequest,
    PayableOrder,
)
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryCheckouts:
    def __init__(self, *checkouts: Checkout) -> None:
        self.by_order: dict[UUID, Checkout] = {c.order_id: c for c in checkouts}

    async def get_for_order(self, order_id: UUID) -> Checkout | None:
        return self.by_order.get(order_id)

    async def upsert(self, checkout: Checkout) -> None:
        self.by_order[checkout.order_id] = checkout


class FakeGateway:
    """Records the requests; answers numbered preferences, or fails when told to."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[CheckoutRequest] = []

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
