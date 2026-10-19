"""Anti-corruption layer: orders' `StockHolds` over inventory's `StockReservations` facade."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.orders.domain.errors import CheckoutCartChanged, CheckoutStockUnavailable
from fragancia_api.shared.kernel import DomainError, Err, Result

_INVENTORY_STOCK_UNAVAILABLE = "INVENTORY_STOCK_UNAVAILABLE"
# The owner switched a presentation to made to order, or removed it, during the checkout: the
# cart the guest confirmed no longer matches the catalog.
_CART_CHANGED_CODES = frozenset(
    {"INVENTORY_PRESENTATION_NOT_STOCKED", "INVENTORY_PRESENTATION_NOT_FOUND"}
)


class StockHoldsAdapter:
    def __init__(self, reservations: StockReservations) -> None:
        self._reservations = reservations

    async def hold(
        self, reference: str, quantities: Mapping[UUID, int], expires_at: datetime
    ) -> Result[UUID, DomainError]:
        result = await self._reservations.reserve(reference, quantities, expires_at)
        match result:
            case Err(error) if error.code == _INVENTORY_STOCK_UNAVAILABLE:
                return Err(CheckoutStockUnavailable(details=error.details))
            case Err(error) if error.code in _CART_CHANGED_CODES:
                return Err(CheckoutCartChanged())
            case _:
                return result

    async def commit(self, reservation_id: UUID) -> Result[None, DomainError]:
        return await self._reservations.commit(reservation_id)

    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        return await self._reservations.release(reservation_id)

    async def return_sold(self, reservation_id: UUID) -> Result[None, DomainError]:
        return await self._reservations.return_sold(reservation_id)
