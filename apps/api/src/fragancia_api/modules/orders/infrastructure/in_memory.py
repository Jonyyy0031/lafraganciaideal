"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.orders.domain.cart import Cart, Offer
from fragancia_api.modules.orders.domain.delivery import DeliverySettings


class InMemoryDeliverySettings:
    """The `DeliverySettingsRepository` and `DeliverySettingsReader` over one value."""

    def __init__(self, settings: DeliverySettings | None = None) -> None:
        self.settings = settings if settings is not None else DeliverySettings.default()

    async def get(self) -> DeliverySettings:
        return self.settings

    async def get_for_update(self) -> DeliverySettings:
        return self.settings

    async def save(self, settings: DeliverySettings) -> None:
        self.settings = settings

    async def current(self) -> DeliverySettings:
        return self.settings


class InMemoryCarts:
    """The `CartRepository` and `CartReader` over one store. Like the SQL adapter it stores and
    hands out copies: a change is only kept by `save`."""

    def __init__(self, *carts: Cart) -> None:
        self.by_id: dict[UUID, Cart] = {cart.id: deepcopy(cart) for cart in carts}

    def _find(self, token_digest: str) -> Cart | None:
        found = next((c for c in self.by_id.values() if c.token_digest == token_digest), None)
        return deepcopy(found) if found is not None else None

    async def get_by_token_for_update(self, token_digest: str) -> Cart | None:
        return self._find(token_digest)

    async def add(self, cart: Cart) -> None:
        self.by_id[cart.id] = deepcopy(cart)

    async def save(self, cart: Cart) -> None:
        self.by_id[cart.id] = deepcopy(cart)

    async def delete_untouched_before(self, cutoff: datetime, limit: int) -> int:
        stale = sorted(
            (c for c in self.by_id.values() if c.updated_at < cutoff), key=lambda c: c.updated_at
        )[:limit]
        for cart in stale:
            del self.by_id[cart.id]
        return len(stale)

    async def by_token(self, token_digest: str) -> Cart | None:
        return self._find(token_digest)


class InMemoryCatalogOffers:
    """The `CatalogOffers` port over a dict the test fills; unknown ids are absent."""

    def __init__(self, *offers: Offer) -> None:
        self.by_id: dict[UUID, Offer] = {offer.presentation_id: offer for offer in offers}

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, Offer]:
        return {i: self.by_id[i] for i in ids if i in self.by_id}
