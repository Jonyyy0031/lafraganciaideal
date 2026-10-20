from datetime import datetime
from typing import Protocol

from fragancia_api.modules.orders.domain.cart import Cart
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.modules.orders.domain.order import Order


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


class OrderRepository(Protocol):
    """Write side of orders. Joins the active transaction."""

    async def next_number(self) -> int:
        """Take the next gapless order number; it rolls back with the transaction."""
        ...

    async def add(self, order: Order) -> None:
        """Insert the order and its lines."""
        ...

    async def get_by_number_for_update(self, number: int) -> Order | None:
        """The order with that number, its row locked `FOR NO KEY UPDATE`
        (a note insert takes `FOR KEY SHARE`, so it does not wait); None when unknown."""
        ...

    async def claim_overdue(self, now: datetime, limit: int) -> list[Order]:
        """Up to `limit` `pending_payment` orders with `payment_due_at < now`, oldest first,
        locked `FOR NO KEY UPDATE SKIP LOCKED` so two runs never take the same one."""
        ...

    async def save(self, order: Order) -> None:
        """Update the order row and insert its `new_history` rows."""
        ...

    async def add_note(self, number: int, actor: str, text: str, at: datetime) -> bool:
        """Store an internal note; False when no order has that number. Takes no lock on the
        order."""
        ...


class CartRepository(Protocol):
    """Write side of carts. Joins the active transaction."""

    async def get_by_token_for_update(self, token_digest: str) -> Cart | None:
        """The cart with that token digest, its row locked `FOR UPDATE`; None when unknown."""
        ...

    async def add(self, cart: Cart) -> None: ...

    async def save(self, cart: Cart) -> None:
        """Persist the cart's header and replace its lines."""
        ...

    async def delete(self, cart: Cart) -> None:
        """Delete the cart (its lines go with it)."""
        ...

    async def delete_untouched_before(self, cutoff: datetime, limit: int) -> int:
        """Delete up to `limit` carts with `updated_at < cutoff` (their lines go with them);
        returns how many."""
        ...
