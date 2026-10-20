from typing import Any, Literal, cast

from sqlalchemy import ColumnElement, Row, func, or_, select

from fragancia_api.modules.orders.contracts import (
    AdminOrderAddress,
    AdminOrderDetail,
    AdminOrderEvent,
    AdminOrderInvoice,
    AdminOrderLine,
    AdminOrderNote,
    AdminOrderPage,
    AdminOrderSummary,
    OrderStatusName,
    RefundStatusName,
)
from fragancia_api.modules.orders.domain.order import format_order_number, parse_order_number
from fragancia_api.modules.orders.infrastructure.tables import (
    order_events,
    order_lines,
    order_notes,
    orders,
)
from fragancia_api.shared.infrastructure.database import Database

_Method = Literal["home", "local", "pickup"]


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _refund(row: Row[Any]) -> RefundStatusName | None:
    return cast(RefundStatusName | None, row.refund_status)


class SqlOrderQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def page(
        self, status: OrderStatusName | None, q: str | None, page: int, size: int
    ) -> AdminOrderPage:
        conditions: list[ColumnElement[bool]] = []
        if status is not None:
            conditions.append(orders.c.status == status)
        if q is not None:
            pattern = _like_pattern(q)
            matches: list[ColumnElement[bool]] = [
                orders.c.customer_email.ilike(pattern, escape="\\"),
                orders.c.customer_name.ilike(pattern, escape="\\"),
            ]
            number = parse_order_number(q)
            if number is not None:
                matches.append(orders.c.number == number)
            conditions.append(or_(*matches))
        statement = (
            select(orders)
            .where(*conditions)
            .order_by(orders.c.number.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
        count = select(func.count()).select_from(orders).where(*conditions)
        async with self._database.reader() as session:
            total = await session.scalar(count) or 0
            rows = (await session.execute(statement)).all()
        return AdminOrderPage(
            items=[
                AdminOrderSummary(
                    number=format_order_number(row.number),
                    created_at=row.created_at,
                    customer_name=row.customer_name,
                    customer_email=row.customer_email,
                    status=cast(OrderStatusName, row.status),
                    delivery_method=cast(_Method, row.delivery_method),
                    total_cents=row.total_cents,
                    refund_status=_refund(row),
                )
                for row in rows
            ],
            total=total,
            page=page,
            size=size,
        )

    async def detail(self, number: int) -> AdminOrderDetail | None:
        async with self._database.reader() as session:
            row = (await session.execute(select(orders).where(orders.c.number == number))).first()
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
            notes = (
                await session.execute(
                    select(order_notes)
                    .where(order_notes.c.order_id == row.id)
                    .order_by(order_notes.c.at.desc(), order_notes.c.id.desc())
                )
            ).all()
        return AdminOrderDetail(
            number=format_order_number(row.number),
            status=cast(OrderStatusName, row.status),
            created_at=row.created_at,
            updated_at=row.updated_at,
            payment_due_at=row.payment_due_at,
            customer_name=row.customer_name,
            customer_email=row.customer_email,
            customer_phone=row.customer_phone,
            delivery_method=cast(_Method, row.delivery_method),
            delivery_cents=row.delivery_cents,
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
            lines=[
                AdminOrderLine(
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
                for line in lines
            ],
            subtotal_cents=row.subtotal_cents,
            total_cents=row.total_cents,
            lead_time_max_days=row.lead_time_max_days,
            cancel_reason=row.cancel_reason,
            refund_status=_refund(row),
            shipping_carrier=row.shipping_carrier,
            tracking_number=row.tracking_number,
            reservation_id=row.reservation_id,
            history=[
                AdminOrderEvent(
                    at=event.at,
                    from_status=cast(OrderStatusName, event.from_status),
                    to_status=cast(OrderStatusName, event.to_status),
                    actor=event.actor,
                    note=event.note,
                    carrier=event.carrier,
                    tracking_number=event.tracking_number,
                )
                for event in events
            ],
            notes=[AdminOrderNote(at=note.at, actor=note.actor, text=note.text) for note in notes],
        )
