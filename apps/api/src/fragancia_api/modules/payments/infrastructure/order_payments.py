"""Anti-corruption layer: payments' `PayableOrders` and `OrderConfirmations` over orders'
`OrderPayments` and `OrderPaymentConfirmations` facades."""

from uuid import UUID

from fragancia_api.modules.orders import OrderPaymentConfirmations, OrderPayments
from fragancia_api.modules.payments.application.ports import (
    Confirmation,
    PayableLine,
    PayableOrder,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class OrderPaymentsAdapter:
    def __init__(self, orders: OrderPayments, confirmations: OrderPaymentConfirmations) -> None:
        self._orders = orders
        self._confirmations = confirmations

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

    async def confirm(
        self, order_id: UUID, payment_id: str, amount_cents: int, currency: str
    ) -> Result[Confirmation, DomainError]:
        match await self._confirmations.confirm(order_id, payment_id, amount_cents, currency):
            case Err(error):
                return Err(error)
            case Ok(confirmation):
                return Ok(
                    Confirmation(outcome=confirmation.outcome.value, number=confirmation.number)
                )
