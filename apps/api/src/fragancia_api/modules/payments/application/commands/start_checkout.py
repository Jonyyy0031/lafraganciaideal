from datetime import timedelta

from fragancia_api.modules.payments.application.ports import (
    CheckoutItem,
    CheckoutRequest,
    PayableOrder,
    PayableOrders,
    PaymentGateway,
)
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.domain.errors import (
    PaymentOrderNotFound,
    PaymentOrderNotPayable,
)
from fragancia_api.modules.payments.domain.repositories import CheckoutRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

PENDING_PAYMENT = "pending_payment"
# A cash or transfer ticket issued from a new link expires 48 hours after the link was created
# (decision 5). A ticket can only be issued while the link is valid, so it expires at most 48
# hours after it is issued.
CASH_TICKET_TTL = timedelta(hours=48)
SHIPPING_ITEM_ID = "shipping"
SHIPPING_TITLE = "Envío"


def checkout_items(order: PayableOrder) -> list[CheckoutItem]:
    """One item per line, plus the shipping when it costs something. Their sum is the order's
    frozen total."""
    items = [
        CheckoutItem(
            id=str(line.presentation_id),
            title=line.label,
            quantity=line.quantity,
            unit_price_cents=line.unit_price_cents,
        )
        for line in order.lines
    ]
    if order.delivery_cents > 0:
        items.append(CheckoutItem(SHIPPING_ITEM_ID, SHIPPING_TITLE, 1, order.delivery_cents))
    total = sum(item.quantity * item.unit_price_cents for item in items)
    if total != order.total_cents:
        raise ValueError("the items must add up to the order total")
    return items


class StartCheckout:
    """Command: the redirect URL that pays an order (decisions 2 and 4, ADR 0004).

    Reuses the stored checkout while it is valid; otherwise creates a Mercado Pago preference
    (outside any transaction, it is an HTTP call) that expires with the order's payment window,
    and stores it. Nothing here moves the order to paid.
    """

    def __init__(
        self,
        *,
        orders: PayableOrders,
        checkouts: CheckoutRepository,
        gateway: PaymentGateway,
        transactions: TransactionRunner,
        clock: Clock,
        back_url: str,
    ) -> None:
        self._orders = orders
        self._checkouts = checkouts
        self._gateway = gateway
        self._transactions = transactions
        self._clock = clock
        self._back_url = back_url

    async def execute(self, access_token: str) -> Result[str, DomainError]:
        order = await self._orders.payable(access_token)
        if order is None:
            return Err(PaymentOrderNotFound())
        now = self._clock.now()
        if order.status != PENDING_PAYMENT or order.payment_due_at <= now:
            return Err(PaymentOrderNotPayable(details={"status": order.status}))

        async def read() -> Result[Checkout | None, DomainError]:
            return Ok(await self._checkouts.get_for_order(order.order_id))

        match await self._transactions.run(read):
            case Ok(stored) if stored is not None and stored.reusable(order.payment_due_at, now):
                return Ok(stored.redirect_url)
            case Err(error):
                return Err(error)
            case _:
                pass

        created = await self._gateway.create_checkout(
            CheckoutRequest(
                order_id=order.order_id,
                number=order.number,
                payer_name=order.customer_name,
                payer_email=order.customer_email,
                items=checkout_items(order),
                expires_at=order.payment_due_at,
                back_url=self._back_url,
                cash_expires_at=now + CASH_TICKET_TTL,
            )
        )
        match created:
            case Err(error):
                return Err(error)
            case Ok(link):
                checkout = Checkout(
                    order_id=order.order_id,
                    preference_id=link.preference_id,
                    redirect_url=link.redirect_url,
                    expires_at=order.payment_due_at,
                    created_at=now,
                )

        async def store() -> Result[str, DomainError]:
            await self._checkouts.upsert(checkout)
            return Ok(checkout.redirect_url)

        return await self._transactions.run(store)
