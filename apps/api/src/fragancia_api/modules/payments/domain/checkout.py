from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

# A link about to expire is not reused: the guest could land on an expired page.
REUSE_MARGIN = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class Checkout:
    """The Mercado Pago preference created for an order (one per order)."""

    order_id: UUID
    preference_id: str
    redirect_url: str
    expires_at: datetime
    created_at: datetime

    def reusable(self, now: datetime) -> bool:
        return self.expires_at - REUSE_MARGIN > now
