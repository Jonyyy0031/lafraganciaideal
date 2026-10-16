"""Anti-corruption layer: orders' `StockHolds` over inventory's `StockReservations` facade."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.orders.domain.errors import CheckoutStockUnavailable
from fragancia_api.shared.kernel import DomainError, Err, Result

_INVENTORY_STOCK_UNAVAILABLE = "INVENTORY_STOCK_UNAVAILABLE"


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
            case _:
                return result
