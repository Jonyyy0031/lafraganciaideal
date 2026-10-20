"""Commands of the customer's order page (decisions 39 and 41)."""

from fragancia_api.modules.orders.application.commands.lifecycle import CancelUnpaidOrder
from fragancia_api.modules.orders.application.ports import (
    CustomerOrderQueries,
    LookupThrottle,
    OrderLinks,
)
from fragancia_api.modules.orders.domain.customer import CustomerEmail
from fragancia_api.modules.orders.domain.errors import (
    OrderLookupNotFound,
    OrderLookupRateLimited,
    OrderNotFound,
)
from fragancia_api.modules.orders.domain.order import (
    CUSTOMER_ACTOR,
    CUSTOMER_CANCEL_REASON,
    LOOKUP_IP_MAX,
    LOOKUP_NUMBER_MAX,
    LOOKUP_WINDOW,
    parse_order_number,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class LookUpOrder:
    """Command: find an order again with its number and the email it was placed with, and answer
    its private-link token (decision 39).

    Attempts are counted per IP and per order number in their own committed transaction, before
    anything is looked up, so a refusal never rolls the count back. An unparsable number, an
    invalid email, an unknown number and a wrong email all answer the same
    `ORDERS_LOOKUP_NOT_FOUND`, so the lookup never reveals which numbers exist."""

    def __init__(
        self,
        *,
        queries: CustomerOrderQueries,
        links: OrderLinks,
        throttle: LookupThrottle,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._queries = queries
        self._links = links
        self._throttle = throttle
        self._transactions = transactions
        self._clock = clock

    async def execute(self, number: str, email: str, ip: str | None) -> Result[str, DomainError]:
        parsed = parse_order_number(number.strip())
        ip_key = f"lookup-ip:{ip or 'unknown'}"

        async def count() -> Result[tuple[int, int | None], DomainError]:
            now = self._clock.now()
            ip_count = await self._throttle.hit(ip_key, now, LOOKUP_WINDOW)
            number_count = (
                await self._throttle.hit(f"lookup-number:{parsed}", now, LOOKUP_WINDOW)
                if parsed is not None
                else None
            )
            return Ok((ip_count, number_count))  # always Ok: the counts must commit

        match await self._transactions.run(count):
            case Err(error):
                return Err(error)
            case Ok((ip_count, number_count)):
                pass
        if ip_count > LOOKUP_IP_MAX or (
            number_count is not None and number_count > LOOKUP_NUMBER_MAX
        ):
            return Err(OrderLookupRateLimited())

        if parsed is None:
            return Err(OrderLookupNotFound())
        match CustomerEmail.create(email):
            case Err(_):
                return Err(OrderLookupNotFound())
            case Ok(valid):
                pass
        found = await self._queries.match(parsed, valid.value)
        if found is None:
            return Err(OrderLookupNotFound())
        return Ok(self._links.token(found[0]))


class CancelOrderByCustomer:
    """Command: the customer cancels their own order from the private link while it is
    `pending_payment` (decision 41). Goes through `CancelUnpaidOrder`, so the reservation is
    released; a paid order is refused with `ORDERS_TRANSITION_INVALID`."""

    def __init__(
        self, *, queries: CustomerOrderQueries, links: OrderLinks, cancel: CancelUnpaidOrder
    ) -> None:
        self._queries = queries
        self._links = links
        self._cancel = cancel

    async def execute(self, token: str) -> Result[None, DomainError]:
        view = await self._queries.by_access_digest(self._links.digest(token))
        number = parse_order_number(view.number) if view is not None else None
        if number is None:
            return Err(OrderNotFound())
        return await self._cancel.execute(number, CUSTOMER_CANCEL_REASON, CUSTOMER_ACTOR)
