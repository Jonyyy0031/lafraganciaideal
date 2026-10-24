from typing import Protocol
from uuid import UUID

from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.domain.payment import Payment


class CheckoutRepository(Protocol):
    """Write side of payments. Joins the active transaction."""

    async def get_for_order(self, order_id: UUID) -> Checkout | None: ...

    async def upsert(self, checkout: Checkout) -> None:
        """Store the checkout, replacing the one the order had."""
        ...


class PaymentRepository(Protocol):
    """Write side of the Mercado Pago payments. Joins the active transaction."""

    async def get_for_update(self, payment_id: str) -> Payment | None:
        """The payment, its row locked `FOR UPDATE`; None when unknown."""
        ...

    async def save(self, payment: Payment) -> None:
        """Insert the payment or replace the stored one (upsert by `payment_id`)."""
        ...
