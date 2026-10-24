from typing import Protocol
from uuid import UUID

from fragancia_api.modules.payments.domain.checkout import Checkout


class CheckoutRepository(Protocol):
    """Write side of payments. Joins the active transaction."""

    async def get_for_order(self, order_id: UUID) -> Checkout | None: ...

    async def upsert(self, checkout: Checkout) -> None:
        """Store the checkout, replacing the one the order had."""
        ...
