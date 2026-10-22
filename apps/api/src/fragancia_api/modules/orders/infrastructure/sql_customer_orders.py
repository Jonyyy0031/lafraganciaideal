from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select

from fragancia_api.modules.orders.application.ports import PayableLine, PayableOrder
from fragancia_api.modules.orders.contracts import (
    AdminOrderAddress,
    AdminOrderInvoice,
    CustomerOrderLine,
    CustomerOrderStep,
    CustomerOrderView,
    OrderStatusName,
    RefundStatusName,
)
from fragancia_api.modules.orders.domain.order import cancelled_by, format_order_number
from fragancia_api.modules.orders.infrastructure.tables import order_events, order_lines, orders
from fragancia_api.shared.infrastructure.database import Database


class SqlCustomerOrders:
    """The customer's page of an order, read by the digest of its private-link token."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def by_access_digest(self, digest: str) -> CustomerOrderView | None:
        async with self._database.reader() as session:
            row = (
                await session.execute(select(orders).where(orders.c.access_token_digest == digest))
            ).first()
            if row is None:
                return None
            lines = (
                await session.execute(
                    select(order_lines)
                    .where(order_lines.c.order_id == row.id)
                    .order_by(order_lines.c.position)
                )
            ).all()
            events = (
                await session.execute(
                    select(order_events)
                    .where(order_events.c.order_id == row.id)
                    .order_by(order_events.c.at, order_events.c.id)
                )
            ).all()
        # The refund mark (cancelled -> cancelled) is not a step: `refund_status` says it.
        moves = [event for event in events if event.from_status != event.to_status]
        cancel = next((event for event in moves if event.to_status == "cancelled"), None)
        return CustomerOrderView(
            number=format_order_number(row.number),
            status=cast(OrderStatusName, row.status),
            created_at=row.created_at,
            payment_due_at=row.payment_due_at,
            customer_name=row.customer_name,
            customer_phone=row.customer_phone,
            lines=[
                CustomerOrderLine(
                    label=line.label,
                    ml=line.ml,
                    quantity=line.quantity,
                    unit_price_cents=line.unit_price_cents,
                    line_total_cents=line.line_total_cents,
                    availability=cast(Literal["in_stock", "made_to_order"], line.availability),
                    lead_time_min_days=line.lead_time_min_days,
                    lead_time_max_days=line.lead_time_max_days,
                )
                for line in lines
            ],
            subtotal_cents=row.subtotal_cents,
            delivery_cents=row.delivery_cents,
            total_cents=row.total_cents,
            lead_time_max_days=row.lead_time_max_days,
            delivery_method=cast(Literal["home", "local", "pickup"], row.delivery_method),
            address=(
                AdminOrderAddress(
                    street=row.address_street,
                    exterior_number=row.address_exterior_number,
                    interior_number=row.address_interior_number,
                    neighborhood=row.address_neighborhood,
                    postal_code=row.address_postal_code,
                    municipality=row.address_municipality,
                    state=row.address_state,
                    references=row.address_references,
                )
                if row.address_street is not None
                else None
            ),
            pickup_address=row.pickup_address,
            pickup_hours=row.pickup_hours,
            invoice=(
                AdminOrderInvoice(
                    rfc=row.invoice_rfc,
                    business_name=row.invoice_business_name,
                    tax_regime=row.invoice_tax_regime,
                    cfdi_use=row.invoice_cfdi_use,
                    postal_code=row.invoice_postal_code,
                )
                if row.invoice_rfc is not None
                else None
            ),
            shipping_carrier=row.shipping_carrier,
            tracking_number=row.tracking_number,
            refund_status=cast(RefundStatusName | None, row.refund_status),
            cancelled_by=cancelled_by(cancel.actor) if cancel is not None else None,
            timeline=[
                CustomerOrderStep(status="pending_payment", at=row.created_at),
                *(
                    CustomerOrderStep(status=cast(OrderStatusName, event.to_status), at=event.at)
                    for event in moves
                ),
            ],
            can_cancel=row.status == "pending_payment",
        )

    async def payable_by_access_digest(self, digest: str) -> PayableOrder | None:
        async with self._database.reader() as session:
            row = (
                await session.execute(select(orders).where(orders.c.access_token_digest == digest))
            ).first()
            if row is None:
                return None
            lines = (
                await session.execute(
                    select(order_lines)
                    .where(order_lines.c.order_id == row.id)
                    .order_by(order_lines.c.position)
                )
            ).all()
        return PayableOrder(
            order_id=row.id,
            number=format_order_number(row.number),
            status=row.status,
            payment_due_at=row.payment_due_at,
            customer_name=row.customer_name,
            customer_email=row.customer_email,
            lines=[
                PayableLine(
                    presentation_id=line.presentation_id,
                    label=line.label,
                    quantity=line.quantity,
                    unit_price_cents=line.unit_price_cents,
                )
                for line in lines
            ],
            delivery_cents=row.delivery_cents,
            total_cents=row.total_cents,
        )

    async def match(self, number: int, email: str) -> tuple[UUID, int] | None:
        """`email` is already normalized; the stored one is too."""
        async with self._database.reader() as session:
            row = (
                await session.execute(
                    select(orders.c.id, orders.c.number).where(
                        orders.c.number == number, orders.c.customer_email == email
                    )
                )
            ).first()
        return (row.id, row.number) if row is not None else None
