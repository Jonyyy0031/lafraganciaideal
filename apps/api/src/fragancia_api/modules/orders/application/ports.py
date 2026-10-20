"""Read-side ports of the orders module (queries run outside any transaction)."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.orders.contracts import (
    AdminOrderDetail,
    AdminOrderPage,
    CustomerOrderView,
    OrderStatusName,
)
from fragancia_api.modules.orders.domain.cart import Cart, Offer
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.modules.orders.domain.order import Order
from fragancia_api.shared.kernel import DomainError, Result


class DeliverySettingsReader(Protocol):
    """Read side: the settings outside any transaction (`Database.reader()`)."""

    async def current(self) -> DeliverySettings:
        """The delivery settings (the default when nothing is stored)."""
        ...


class CatalogOffers(Protocol):
    """What the catalog offers buyers, owned by orders and implemented over the catalog's
    public facade."""

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, Offer]:
        """The known presentations by id, priced at `now`; unknown ids are absent. The stock
        figure lags inventory by about 2 s: it is for display, never for reserving."""
        ...


class CartTokens(Protocol):
    """The cart cookie's token: random, with only its digest stored."""

    def new(self) -> str: ...

    def digest(self, token: str) -> str: ...


class OrderLinks(Protocol):
    """The order's private-link token (decision 31): derived from the order id with a server
    secret, so it can be regenerated and is never stored; only its digest is."""

    def token(self, order_id: UUID) -> str: ...

    def digest(self, token: str) -> str: ...


class StockHolds(Protocol):
    """Reserve units for an order, implemented over inventory's `StockReservations`. Joins the
    caller's transaction; all or nothing."""

    async def hold(
        self, reference: str, quantities: Mapping[UUID, int], expires_at: datetime
    ) -> Result[UUID, DomainError]:
        """The reservation id; a shortage comes back as `CheckoutStockUnavailable`."""
        ...

    async def commit(self, reservation_id: UUID) -> Result[None, DomainError]:
        """Sell the reservation's units (the order was paid). A refusal comes back with
        inventory's own code and details."""
        ...

    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        """Give the units back (an unpaid order cancelled); idempotent on an expired one."""
        ...

    async def return_sold(self, reservation_id: UUID) -> Result[None, DomainError]:
        """Put the units of a sold reservation back on hand (a paid order cancelled)."""
        ...


class OrderReader(Protocol):
    """Read side: orders outside any transaction (`Database.reader()`)."""

    async def placed_from_cart(self, cart_token_digest: str, since: datetime) -> Order | None:
        """The newest order placed from the cart with that token digest that is still
        `pending_payment` and was created at or after `since`; None when there is none. Lets a
        retried checkout get its order back (decision 31)."""
        ...


class OrderQueries(Protocol):
    """Read side of the panel: orders outside any transaction (`Database.reader()`)."""

    async def page(
        self, status: OrderStatusName | None, q: str | None, page: int, size: int
    ) -> AdminOrderPage:
        """Orders, newest number first. `q` (already trimmed, non-empty) matches `LFI-1001`,
        `1001`, an email substring or a name substring, case-insensitively."""
        ...

    async def detail(self, number: int) -> AdminOrderDetail | None: ...


class CartReader(Protocol):
    """Read side: a cart outside any transaction (`Database.reader()`)."""

    async def by_token(self, token_digest: str) -> Cart | None: ...


class CustomerOrderQueries(Protocol):
    """Read side of the customer's page (`Database.reader()`)."""

    async def by_access_digest(self, digest: str) -> CustomerOrderView | None: ...

    async def match(self, number: int, email: str) -> tuple[UUID, int] | None:
        """The order id and number when that number exists and its stored email equals `email`
        (already normalized); None otherwise."""
        ...


class LookupThrottle(Protocol):
    """Fixed-window attempt counters for the order lookup. Joins the active transaction."""

    async def hit(self, key: str, now: datetime, window: timedelta) -> int:
        """Count one attempt; the attempts in the current window, this one included."""
        ...
