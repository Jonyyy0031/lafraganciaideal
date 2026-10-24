from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Checkout:
    """The Mercado Pago preference created for an order (one per order)."""

    order_id: UUID
    preference_id: str
    redirect_url: str
    expires_at: datetime
    created_at: datetime

    def reusable(self, payment_due_at: datetime, now: datetime) -> bool:
        """Valid while it expires with the order's current deadline and has not expired. A new
        link is made only when there is none or the deadline changed."""
        return self.expires_at == payment_due_at and self.expires_at > now
