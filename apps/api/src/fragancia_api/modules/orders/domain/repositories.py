from datetime import datetime
from typing import Protocol

from fragancia_api.modules.orders.domain.cart import Cart
from fragancia_api.modules.orders.domain.delivery import DeliverySettings


class DeliverySettingsRepository(Protocol):
    """Write side. Joins the active transaction."""

    async def get(self) -> DeliverySettings:
        """The delivery settings (the default when nothing is stored)."""
        ...

    async def get_for_update(self) -> DeliverySettings:
        """The settings with their row locked `FOR UPDATE` until the transaction ends. The SQL
        adapter first inserts the default row when none is stored, so there is always a row to
        lock."""
        ...

    async def save(self, settings: DeliverySettings) -> None: ...


class CartRepository(Protocol):
    """Write side of carts. Joins the active transaction."""

    async def get_by_token_for_update(self, token_digest: str) -> Cart | None:
        """The cart with that token digest, its row locked `FOR UPDATE`; None when unknown."""
        ...

    async def add(self, cart: Cart) -> None: ...

    async def save(self, cart: Cart) -> None:
        """Persist the cart's header and replace its lines."""
        ...

    async def delete_untouched_before(self, cutoff: datetime, limit: int) -> int:
        """Delete up to `limit` carts with `updated_at < cutoff` (their lines go with them);
        returns how many."""
        ...
