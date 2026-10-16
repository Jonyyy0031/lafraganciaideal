"""The order: a frozen copy of what the guest bought, the delivery and the invoice data. Plan 003
only creates it (`pending_payment`); the other states come with the lifecycle."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Literal
from uuid import UUID

from fragancia_api.modules.orders.domain.customer import (
    Address,
    CustomerEmail,
    CustomerName,
    Invoice,
    Phone,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod
from fragancia_api.shared.kernel import AggregateRoot, DomainEvent

ORDER_NUMBER_PREFIX = "LFI-"
FIRST_ORDER_NUMBER = 1001
PAYMENT_WINDOW = timedelta(minutes=30)


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"


def format_order_number(number: int) -> str:
    return f"{ORDER_NUMBER_PREFIX}{number}"


@dataclass(frozen=True, slots=True)
class OrderLine:
    """The frozen copy of the catalog offer at checkout (decision 11)."""

    presentation_id: UUID
    perfume_id: UUID
    label: str
    ml: int
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    quantity: int
    unit_price_cents: int
    regular_price_cents: int
    line_total_cents: int


@dataclass(frozen=True, slots=True)
class Delivery:
    method: DeliveryMethod
    cost_cents: int
    address: Address | None
    pickup_address: str | None
    pickup_hours: str | None


@dataclass(frozen=True, kw_only=True)
class OrderPlaced(DomainEvent):
    name: ClassVar[str] = "orders.order.placed"
    order_id: UUID
    number: int


class Order(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        number: int,
        status: OrderStatus,
        access_token_digest: str,
        name: CustomerName,
        email: CustomerEmail,
        phone: Phone,
        delivery: Delivery,
        invoice: Invoice | None,
        lines: Sequence[OrderLine],
        subtotal_cents: int,
        total_cents: int,
        lead_time_max_days: int | None,
        reservation_id: UUID | None,
        payment_due_at: datetime,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        super().__init__()
        self.id = id
        self.number = number
        self.status = status
        self.access_token_digest = access_token_digest
        self.name = name
        self.email = email
        self.phone = phone
        self.delivery = delivery
        self.invoice = invoice
        self.lines: list[OrderLine] = list(lines)
        self.subtotal_cents = subtotal_cents
        self.total_cents = total_cents
        self.lead_time_max_days = lead_time_max_days
        self.reservation_id = reservation_id
        self.payment_due_at = payment_due_at
        self.created_at = created_at
        self.updated_at = updated_at

    @classmethod
    def place(
        cls,
        *,
        id: UUID,
        number: int,
        access_token_digest: str,
        name: CustomerName,
        email: CustomerEmail,
        phone: Phone,
        delivery: Delivery,
        invoice: Invoice | None,
        lines: Sequence[OrderLine],
        reservation_id: UUID | None,
        payment_due_at: datetime,
        now: datetime,
    ) -> Order:
        subtotal = sum(line.line_total_cents for line in lines)
        lead_times = [
            line.lead_time_max_days
            for line in lines
            if line.availability == "made_to_order" and line.lead_time_max_days is not None
        ]
        order = cls(
            id=id,
            number=number,
            status=OrderStatus.PENDING_PAYMENT,
            access_token_digest=access_token_digest,
            name=name,
            email=email,
            phone=phone,
            delivery=delivery,
            invoice=invoice,
            lines=lines,
            subtotal_cents=subtotal,
            total_cents=subtotal + delivery.cost_cents,
            lead_time_max_days=max(lead_times) if lead_times else None,
            reservation_id=reservation_id,
            payment_due_at=payment_due_at,
            created_at=now,
            updated_at=now,
        )
        order.record(OrderPlaced(order_id=id, number=number))
        return order
