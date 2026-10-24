"""Anti-corruption layer: payments' `PayableOrders` over orders' `OrderPayments` facade."""

from fragancia_api.modules.orders import OrderPayments
from fragancia_api.modules.payments.application.ports import PayableLine, PayableOrder


class OrderPaymentsAdapter:
    def __init__(self, orders: OrderPayments) -> None:
        self._orders = orders

    async def payable(self, access_token: str) -> PayableOrder | None:
        order = await self._orders.payable(access_token)
        if order is None:
            return None
        return PayableOrder(
            order_id=order.order_id,
            number=order.number,
            status=order.status,
            payment_due_at=order.payment_due_at,
            customer_name=order.customer_name,
            customer_email=order.customer_email,
            lines=[
                PayableLine(
                    presentation_id=line.presentation_id,
                    label=line.label,
                    quantity=line.quantity,
                    unit_price_cents=line.unit_price_cents,
                )
                for line in order.lines
            ],
            delivery_cents=order.delivery_cents,
            total_cents=order.total_cents,
        )
