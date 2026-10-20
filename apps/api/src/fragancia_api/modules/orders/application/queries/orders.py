from fragancia_api.modules.orders.application.ports import OrderQueries
from fragancia_api.modules.orders.contracts import AdminOrderDetail, AdminOrderPage, OrderStatusName
from fragancia_api.modules.orders.domain.errors import OrderNotFound
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class ListOrders:
    """Query: the panel's order list, newest number first, paginated. `status` filters by one
    status; `q` (trimmed, ignored when blank) matches `LFI-1001`, `1001`, an email substring or
    a name substring, case-insensitively."""

    def __init__(self, queries: OrderQueries) -> None:
        self._queries = queries

    async def execute(
        self, *, status: OrderStatusName | None, q: str | None, page: int, size: int
    ) -> AdminOrderPage:
        text = q.strip() if q is not None else ""
        return await self._queries.page(status, text or None, page, size)


class GetOrder:
    """Query: one order for the panel, with its lines, address, invoice, history (oldest
    first), notes (newest first), refund status, carrier, tracking and reservation id."""

    def __init__(self, queries: OrderQueries) -> None:
        self._queries = queries

    async def execute(self, number: int) -> Result[AdminOrderDetail, DomainError]:
        match await self._queries.detail(number):
            case None:
                return Err(OrderNotFound())
            case found:
                return Ok(found)
