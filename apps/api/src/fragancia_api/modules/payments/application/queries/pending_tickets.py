from fragancia_api.modules.payments.application.ports import PayableOrders, PaymentQueries
from fragancia_api.modules.payments.contracts import PendingTickets
from fragancia_api.modules.payments.domain.errors import PaymentOrderNotFound
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.kernel import Err, Ok, Result

PENDING_PAYMENT = "pending_payment"


class GetPendingTickets:
    """Query: the order's cash and transfer tickets that can still be paid (decision 16), for
    the order page. An order that is not waiting for payment has none."""

    def __init__(self, *, orders: PayableOrders, queries: PaymentQueries, clock: Clock) -> None:
        self._orders = orders
        self._queries = queries
        self._clock = clock

    async def execute(self, access_token: str) -> Result[PendingTickets, PaymentOrderNotFound]:
        order = await self._orders.payable(access_token)
        if order is None:
            return Err(PaymentOrderNotFound())
        if order.status != PENDING_PAYMENT:
            return Ok(PendingTickets(items=[]))
        found = await self._queries.pending_for_order(order.order_id, self._clock.now())
        # A ticket never pays the order after the order's own deadline: then the order is
        # cancelled and the ticket with it (decisions 19 and 20).
        items = [
            ticket.model_copy(update={"expires_at": min(ticket.expires_at, order.payment_due_at)})
            for ticket in found
        ]
        return Ok(PendingTickets(items=items))
