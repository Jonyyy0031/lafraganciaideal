"""Read-side ports of the orders module (queries run outside any transaction)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.orders.domain.cart import Cart, Offer
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
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


class OrderAccessTokens(Protocol):
    """The order's private-link token: random, with only its digest stored."""

    def new(self) -> str: ...

    def digest(self, token: str) -> str: ...


class StockHolds(Protocol):
    """Reserve units for an order, implemented over inventory's `StockReservations`. Joins the
    caller's transaction; all or nothing."""

    async def hold(
        self, reference: str, quantities: Mapping[UUID, int], expires_at: datetime
    ) -> Result[UUID, DomainError]:
        """The reservation id; a shortage comes back as `CheckoutStockUnavailable`."""
        ...


class CartReader(Protocol):
    """Read side: a cart outside any transaction (`Database.reader()`)."""

    async def by_token(self, token_digest: str) -> Cart | None: ...
