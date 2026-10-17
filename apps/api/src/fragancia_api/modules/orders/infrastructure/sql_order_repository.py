from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import Row, insert, select, text

from fragancia_api.modules.orders.domain.customer import (
    Address,
    CustomerEmail,
    CustomerName,
    Invoice,
    Phone,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod, PostalCode
from fragancia_api.modules.orders.domain.order import Delivery, Order, OrderLine, OrderStatus
from fragancia_api.modules.orders.infrastructure.tables import order_lines, orders
from fragancia_api.shared.infrastructure.database import Database


class SqlOrderRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def next_number(self) -> int:
        # The counter row stays locked until the transaction ends, so numbers are gapless: a
        # rollback gives the number back.
        result = await self._database.session.execute(
            text(
                "UPDATE orders.order_numbers SET last_number = last_number + 1 "
                "WHERE id = 1 RETURNING last_number"
            )
        )
        return int(result.scalar_one())

    async def add(self, order: Order) -> None:
        session = self._database.session
        delivery = order.delivery
        address = delivery.address
        invoice = order.invoice
        await session.execute(
            insert(orders).values(
                id=order.id,
                number=order.number,
                status=order.status.value,
                access_token_digest=order.access_token_digest,
                cart_token_digest=order.cart_token_digest,
                customer_name=order.name.value,
                customer_email=order.email.value,
                customer_phone=order.phone.value,
                delivery_method=delivery.method.value,
                delivery_cents=delivery.cost_cents,
                address_street=address.street if address else None,
                address_exterior_number=address.exterior_number if address else None,
                address_interior_number=address.interior_number if address else None,
                address_neighborhood=address.neighborhood if address else None,
                address_postal_code=address.postal_code.value if address else None,
                address_municipality=address.municipality if address else None,
                address_state=address.state if address else None,
                address_references=address.references if address else None,
                pickup_address=delivery.pickup_address,
                pickup_hours=delivery.pickup_hours,
                invoice_rfc=invoice.rfc if invoice else None,
                invoice_business_name=invoice.business_name if invoice else None,
                invoice_tax_regime=invoice.tax_regime if invoice else None,
                invoice_cfdi_use=invoice.cfdi_use if invoice else None,
                invoice_postal_code=invoice.postal_code.value if invoice else None,
                subtotal_cents=order.subtotal_cents,
                total_cents=order.total_cents,
                lead_time_max_days=order.lead_time_max_days,
                reservation_id=order.reservation_id,
                payment_due_at=order.payment_due_at,
                created_at=order.created_at,
                updated_at=order.updated_at,
            )
        )
        if order.lines:
            await session.execute(
                insert(order_lines),
                [
                    {
                        "order_id": order.id,
                        "position": position,
                        "presentation_id": line.presentation_id,
                        "perfume_id": line.perfume_id,
                        "label": line.label,
                        "ml": line.ml,
                        "availability": line.availability,
                        "lead_time_min_days": line.lead_time_min_days,
                        "lead_time_max_days": line.lead_time_max_days,
                        "quantity": line.quantity,
                        "unit_price_cents": line.unit_price_cents,
                        "regular_price_cents": line.regular_price_cents,
                        "line_total_cents": line.line_total_cents,
                    }
                    for position, line in enumerate(order.lines)
                ],
            )


class SqlOrderReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def placed_from_cart(self, cart_token_digest: str, since: datetime) -> Order | None:
        async with self._database.reader() as session:
            row = (
                await session.execute(
                    select(orders)
                    .where(
                        orders.c.cart_token_digest == cart_token_digest,
                        orders.c.status == OrderStatus.PENDING_PAYMENT.value,
                        orders.c.created_at >= since,
                    )
                    .order_by(orders.c.created_at.desc(), orders.c.number.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            line_rows = (
                await session.execute(
                    select(order_lines)
                    .where(order_lines.c.order_id == row.id)
                    .order_by(order_lines.c.position)
                )
            ).all()
        return _to_domain(row, line_rows)


def _to_domain(row: Row[Any], line_rows: Sequence[Row[Any]]) -> Order:
    address = (
        Address(
            street=row.address_street,
            exterior_number=row.address_exterior_number,
            interior_number=row.address_interior_number,
            neighborhood=row.address_neighborhood,
            postal_code=PostalCode(row.address_postal_code),
            municipality=row.address_municipality,
            state=row.address_state,
            references=row.address_references,
        )
        if row.address_street is not None
        else None
    )
    invoice = (
        Invoice(
            rfc=row.invoice_rfc,
            business_name=row.invoice_business_name,
            tax_regime=row.invoice_tax_regime,
            cfdi_use=row.invoice_cfdi_use,
            postal_code=PostalCode(row.invoice_postal_code),
        )
        if row.invoice_rfc is not None
        else None
    )
    return Order(
        id=row.id,
        number=row.number,
        status=OrderStatus(row.status),
        access_token_digest=row.access_token_digest,
        cart_token_digest=row.cart_token_digest,
        name=CustomerName(row.customer_name),
        email=CustomerEmail(row.customer_email),
        phone=Phone(row.customer_phone),
        delivery=Delivery(
            method=DeliveryMethod(row.delivery_method),
            cost_cents=row.delivery_cents,
            address=address,
            pickup_address=row.pickup_address,
            pickup_hours=row.pickup_hours,
        ),
        invoice=invoice,
        lines=[
            OrderLine(
                presentation_id=line.presentation_id,
                perfume_id=line.perfume_id,
                label=line.label,
                ml=line.ml,
                availability=cast(Literal["in_stock", "made_to_order"], line.availability),
                lead_time_min_days=line.lead_time_min_days,
                lead_time_max_days=line.lead_time_max_days,
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                regular_price_cents=line.regular_price_cents,
                line_total_cents=line.line_total_cents,
            )
            for line in line_rows
        ],
        subtotal_cents=row.subtotal_cents,
        total_cents=row.total_cents,
        lead_time_max_days=row.lead_time_max_days,
        reservation_id=row.reservation_id,
        payment_due_at=row.payment_due_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
