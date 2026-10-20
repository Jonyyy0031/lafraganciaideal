from fragancia_api.modules.orders.application.ports import CustomerOrderQueries, OrderLinks
from fragancia_api.modules.orders.contracts import CustomerOrderView
from fragancia_api.modules.orders.domain.errors import OrderNotFound
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class GetCustomerOrder:
    """Query: the customer's page of an order, found by its private-link token (decisions 13
    and 40). The token is a secret: only its digest is looked up."""

    def __init__(self, *, queries: CustomerOrderQueries, links: OrderLinks) -> None:
        self._queries = queries
        self._links = links

    async def execute(self, token: str) -> Result[CustomerOrderView, DomainError]:
        match await self._queries.by_access_digest(self._links.digest(token)):
            case None:
                return Err(OrderNotFound())
            case found:
                return Ok(found)
