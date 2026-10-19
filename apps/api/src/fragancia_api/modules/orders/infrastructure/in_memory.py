"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.orders.domain.cart import Cart, Offer
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.modules.orders.domain.errors import CheckoutStockUnavailable
from fragancia_api.modules.orders.domain.order import Order, OrderEvent, OrderStatus
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id


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

    async def delete(self, cart: Cart) -> None:
        self.by_id.pop(cart.id, None)

    async def delete_untouched_before(self, cutoff: datetime, limit: int) -> int:
        stale = sorted(
            (c for c in self.by_id.values() if c.updated_at < cutoff), key=lambda c: c.updated_at
        )[:limit]
        for cart in stale:
            del self.by_id[cart.id]
        return len(stale)

    async def by_token(self, token_digest: str) -> Cart | None:
        return self._find(token_digest)


class InMemoryOrders:
    """The `OrderRepository` over a list; the counter starts at 1000 like the migration's seed."""

    def __init__(self, last_number: int = 1000) -> None:
        self.last_number = last_number
        self.orders: list[Order] = []
        # What the SQL adapter keeps in `order_events` and `order_notes`, by order id.
        self.history: dict[UUID, list[OrderEvent]] = {}
        self.notes: dict[UUID, list[tuple[datetime, str, str]]] = {}

    async def next_number(self) -> int:
        self.last_number += 1
        return self.last_number

    async def add(self, order: Order) -> None:
        self.orders.append(deepcopy(order))

    def _find(self, number: int) -> Order | None:
        return next((o for o in self.orders if o.number == number), None)

    async def get_by_number_for_update(self, number: int) -> Order | None:
        found = self._find(number)
        return deepcopy(found) if found is not None else None

    async def claim_overdue(self, now: datetime, limit: int) -> list[Order]:
        due = sorted(
            (
                o
                for o in self.orders
                if o.status == OrderStatus.PENDING_PAYMENT and o.payment_due_at < now
            ),
            key=lambda o: (o.payment_due_at, o.number),
        )[:limit]
        return [deepcopy(order) for order in due]

    async def save(self, order: Order) -> None:
        """Like the SQL adapter it keeps a copy, appends the new history to the stored one and
        forgets it on the order it was given."""
        stored = deepcopy(order)
        position = next(i for i, o in enumerate(self.orders) if o.id == order.id)
        self.history.setdefault(order.id, []).extend(order.new_history)
        order.new_history.clear()
        stored.new_history.clear()
        self.orders[position] = stored

    async def add_note(self, number: int, actor: str, text: str, at: datetime) -> bool:
        found = self._find(number)
        if found is None:
            return False
        self.notes.setdefault(found.id, []).append((at, actor, text))
        return True

    async def placed_from_cart(self, cart_token_digest: str, since: datetime) -> Order | None:
        found = [
            order
            for order in self.orders
            if order.cart_token_digest == cart_token_digest
            and order.status == OrderStatus.PENDING_PAYMENT
            and order.created_at >= since
        ]
        return deepcopy(max(found, key=lambda o: (o.created_at, o.number))) if found else None


class InMemoryStockHolds:
    """The `StockHolds` port: holds succeed with a fresh id, or refuse with the given short
    lines. Every hold is recorded as (reference, quantities, expires_at) in `calls`; every
    commit, release and return as (operation, reservation id) in `operations`. `refusal`, when
    set, makes commit, release and return fail with it."""

    def __init__(
        self,
        short_lines: list[dict[str, object]] | None = None,
        refusal: DomainError | None = None,
    ) -> None:
        self.short_lines = short_lines
        self.refusal = refusal
        self.calls: list[tuple[str, dict[UUID, int], datetime]] = []
        self.operations: list[tuple[str, UUID]] = []

    async def commit(self, reservation_id: UUID) -> Result[None, DomainError]:
        return self._operate("commit", reservation_id)

    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        return self._operate("release", reservation_id)

    async def return_sold(self, reservation_id: UUID) -> Result[None, DomainError]:
        return self._operate("return_sold", reservation_id)

    def _operate(self, operation: str, reservation_id: UUID) -> Result[None, DomainError]:
        self.operations.append((operation, reservation_id))
        if self.refusal is not None:
            return Err(self.refusal)
        return Ok(None)

    async def hold(
        self, reference: str, quantities: Mapping[UUID, int], expires_at: datetime
    ) -> Result[UUID, DomainError]:
        self.calls.append((reference, dict(quantities), expires_at))
        if self.short_lines is not None:
            return Err(CheckoutStockUnavailable(details={"lines": self.short_lines}))
        return Ok(new_id())


class InMemoryCatalogOffers:
    """The `CatalogOffers` port over a dict the test fills; unknown ids are absent."""

    def __init__(self, *offers: Offer) -> None:
        self.by_id: dict[UUID, Offer] = {offer.presentation_id: offer for offer in offers}

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, Offer]:
        return {i: self.by_id[i] for i in ids if i in self.by_id}
