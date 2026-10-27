from datetime import datetime
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

    async def close(self, order_id: UUID, at: datetime) -> None:
        """Set the stored checkout's `expires_at` to `at`, so it is never reused (decision 18).
        Nothing happens when the order has none."""
        ...


class PaymentRepository(Protocol):
    """Write side of the Mercado Pago payments. Joins the active transaction."""

    async def claim(self, payment: Payment) -> Payment:
        """Lock the payment's row `FOR UPDATE` and return the stored payment.

        The given payment is inserted first when its id is unknown, so the row lock exists even
        for a payment seen for the first time and a concurrent delivery of the same payment
        waits on it. When the row was already there, the stored payment is returned as it is
        (its `outcome` is kept) and the given one is discarded.
        """
        ...

    async def save(self, payment: Payment) -> None:
        """Insert the payment or replace the stored one (upsert by `payment_id`)."""
        ...
