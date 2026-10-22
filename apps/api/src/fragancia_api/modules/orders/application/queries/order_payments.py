from fragancia_api.modules.orders.application.ports import (
    CustomerOrderQueries,
    OrderLinks,
    PayableLine,
    PayableOrder,
)

__all__ = ["OrderPayments", "PayableLine", "PayableOrder"]


class OrderPayments:
    """Query facade: the only way other modules (payments) read an order to charge it.

    `payable(access_token)` answers the order behind the private-link token, with the frozen
    figures a payment needs, or None when no order matches. The token is a secret: only its
    digest is looked up.
    """

    def __init__(self, *, queries: CustomerOrderQueries, links: OrderLinks) -> None:
        self._queries = queries
        self._links = links

    async def payable(self, access_token: str) -> PayableOrder | None:
        return await self._queries.payable_by_access_digest(self._links.digest(access_token))
