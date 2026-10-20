"""The order: a frozen copy of what the guest bought, the delivery and the invoice data, and its
lifecycle. It is created `pending_payment` and only moves forward (decision 35):

    pending_payment -> paid | cancelled
    paid            -> preparing (optional) | shipped | ready_for_pickup | cancelled
    preparing       -> shipped | ready_for_pickup
    shipped | ready_for_pickup -> delivered

Every move appends one `OrderEvent` (the history row the repository writes) and records
`orders.order.status_changed`. Refusals are `Err`; the callers pair a move with the inventory
call it needs inside one transaction."""

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
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod, Pickup
from fragancia_api.modules.orders.domain.errors import (
    CustomerDataInvalid,
    OrderDataInvalid,
    OrderTransitionInvalid,
)
from fragancia_api.shared.kernel import AggregateRoot, DomainError, DomainEvent, Err, Ok, Result

ORDER_NUMBER_PREFIX = "LFI-"
FIRST_ORDER_NUMBER = 1001
PAYMENT_WINDOW = timedelta(minutes=30)

NOTE_MAX = 300
REASON_MAX = 300
CARRIER_MAX = 60
TRACKING_NUMBER_MAX = 60
AUTO_CANCEL_REASON = "No pagado a tiempo"
SYSTEM_ACTOR = "system"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PREPARING = "preparing"
    SHIPPED = "shipped"
    READY_FOR_PICKUP = "ready_for_pickup"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class RefundStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


# The state machine (decisions 9 and 35): the statuses an order can move to from each one.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: frozenset({OrderStatus.PAID, OrderStatus.CANCELLED}),
    OrderStatus.PAID: frozenset(
        {
            OrderStatus.PREPARING,
            OrderStatus.SHIPPED,
            OrderStatus.READY_FOR_PICKUP,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.PREPARING: frozenset({OrderStatus.SHIPPED, OrderStatus.READY_FOR_PICKUP}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.READY_FOR_PICKUP: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


NOTE_TEXT_MAX = 1000


def parse_order_number(raw: str) -> int | None:
    """`LFI-1001` or `1001` (any case, no spaces) to 1001; None for anything else."""
    value = raw.upper().removeprefix(ORDER_NUMBER_PREFIX)
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        return None
    return int(value)


def clean_note_text(raw: str) -> Result[str, OrderDataInvalid]:
    """An internal note keeps its line breaks; only the ends are trimmed (1 to 1000 chars)."""
    value = raw.strip()
    if not 1 <= len(value) <= NOTE_TEXT_MAX:
        return Err(OrderDataInvalid(details={"field": "text", "max": NOTE_TEXT_MAX}))
    return Ok(value)


def _text(raw: str, maximum: int, field: str) -> Result[str, OrderDataInvalid]:
    """The text with its whitespace collapsed, 1 to `maximum` characters."""
    value = " ".join(raw.split())
    if not 1 <= len(value) <= maximum:
        return Err(OrderDataInvalid(details={"field": field, "max": maximum}))
    return Ok(value)


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

    @classmethod
    def create(
        cls,
        *,
        presentation_id: UUID,
        perfume_id: UUID,
        label: str,
        ml: int,
        availability: Literal["in_stock", "made_to_order"],
        lead_time_min_days: int | None,
        lead_time_max_days: int | None,
        quantity: int,
        unit_price_cents: int,
        regular_price_cents: int,
    ) -> OrderLine:
        """The line total is the unit price times the quantity."""
        return cls(
            presentation_id=presentation_id,
            perfume_id=perfume_id,
            label=label,
            ml=ml,
            availability=availability,
            lead_time_min_days=lead_time_min_days,
            lead_time_max_days=lead_time_max_days,
            quantity=quantity,
            unit_price_cents=unit_price_cents,
            regular_price_cents=regular_price_cents,
            line_total_cents=unit_price_cents * quantity,
        )


@dataclass(frozen=True, slots=True)
class Delivery:
    method: DeliveryMethod
    cost_cents: int
    address: Address | None
    pickup_address: str | None
    pickup_hours: str | None

    @classmethod
    def create(
        cls,
        method: DeliveryMethod,
        cost_cents: int,
        address: Address | None,
        pickup: Pickup | None,
    ) -> Result[Delivery, CustomerDataInvalid]:
        """Home and local delivery need an address; pickup must not carry one and copies the
        pickup point as it is at checkout."""
        if (method == DeliveryMethod.PICKUP) != (address is None):
            return Err(CustomerDataInvalid(details={"field": "address"}))
        if method == DeliveryMethod.PICKUP and pickup is not None:
            return Ok(cls(method, cost_cents, None, pickup.address, pickup.hours))
        return Ok(cls(method, cost_cents, address, None, None))


@dataclass(frozen=True, kw_only=True)
class OrderPlaced(DomainEvent):
    name: ClassVar[str] = "orders.order.placed"
    order_id: UUID
    number: int


@dataclass(frozen=True, kw_only=True)
class OrderStatusChanged(DomainEvent):
    """Published for every move, including the refund mark (from and to are then both
    `cancelled`); the emails (plan 006) subscribe to it by name."""

    name: ClassVar[str] = "orders.order.status_changed"
    order_id: UUID
    number: int
    from_status: str
    to_status: str


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """One history row: who moved the order, when, and the data entered."""

    at: datetime
    from_status: OrderStatus
    to_status: OrderStatus
    actor: str
    note: str | None = None
    carrier: str | None = None
    tracking_number: str | None = None


class Order(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        number: int,
        status: OrderStatus,
        access_token_digest: str,
        cart_token_digest: str | None,
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
        cancel_reason: str | None = None,
        refund_status: RefundStatus | None = None,
        shipping_carrier: str | None = None,
        tracking_number: str | None = None,
    ) -> None:
        super().__init__()
        self.id = id
        self.number = number
        self.status = status
        self.access_token_digest = access_token_digest
        # The digest of the cart cookie the order was placed from (None for rows that predate
        # migration 0020); a retried checkout finds its order through it (decision 31).
        self.cart_token_digest = cart_token_digest
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
        self.cancel_reason = cancel_reason
        self.refund_status = refund_status
        self.shipping_carrier = shipping_carrier
        self.tracking_number = tracking_number
        # The history rows of the moves made since the order was loaded; the repository writes
        # them. Older rows are not loaded on the write side.
        self.new_history: list[OrderEvent] = []

    @classmethod
    def place(
        cls,
        *,
        id: UUID,
        number: int,
        access_token_digest: str,
        cart_token_digest: str,
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
            cart_token_digest=cart_token_digest,
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

    def mark_paid(self, note: str, actor: str, now: datetime) -> Result[None, DomainError]:
        """Paid by hand until the payments initiative exists; the note says how (decision 32)."""
        if not self._can_move_to(OrderStatus.PAID):
            return Err(self._refused(OrderStatus.PAID))
        match _text(note, NOTE_MAX, "note"):
            case Err(invalid):
                return Err(invalid)
            case Ok(clean):
                pass
        self._move(OrderStatus.PAID, actor, now, note=clean)
        return Ok(None)

    def start_preparing(self, actor: str, now: datetime) -> Result[None, DomainError]:
        if not self._can_move_to(OrderStatus.PREPARING):
            return Err(self._refused(OrderStatus.PREPARING))
        self._move(OrderStatus.PREPARING, actor, now)
        return Ok(None)

    def ship(
        self, carrier: str | None, tracking_number: str | None, actor: str, now: datetime
    ) -> Result[None, DomainError]:
        """Home delivery needs the carrier and the tracking number; local delivery carries
        neither (decision 33). Pickup orders are never shipped."""
        if self.delivery.method == DeliveryMethod.PICKUP or not self._can_move_to(
            OrderStatus.SHIPPED
        ):
            return Err(self._refused(OrderStatus.SHIPPED))
        clean_carrier: str | None = None
        clean_tracking: str | None = None
        if self.delivery.method == DeliveryMethod.HOME:
            match _text(carrier or "", CARRIER_MAX, "carrier"):
                case Err(invalid):
                    return Err(invalid)
                case Ok(clean_carrier):
                    pass
            match _text(tracking_number or "", TRACKING_NUMBER_MAX, "tracking_number"):
                case Err(invalid):
                    return Err(invalid)
                case Ok(clean_tracking):
                    pass
        elif carrier is not None or tracking_number is not None:
            field = "carrier" if carrier is not None else "tracking_number"
            return Err(OrderDataInvalid(details={"field": field}))
        self.shipping_carrier = clean_carrier
        self.tracking_number = clean_tracking
        self._move(
            OrderStatus.SHIPPED, actor, now, carrier=clean_carrier, tracking_number=clean_tracking
        )
        return Ok(None)

    def ready_for_pickup(self, actor: str, now: datetime) -> Result[None, DomainError]:
        if self.delivery.method != DeliveryMethod.PICKUP or not self._can_move_to(
            OrderStatus.READY_FOR_PICKUP
        ):
            return Err(self._refused(OrderStatus.READY_FOR_PICKUP))
        self._move(OrderStatus.READY_FOR_PICKUP, actor, now)
        return Ok(None)

    def deliver(self, actor: str, now: datetime) -> Result[None, DomainError]:
        if not self._can_move_to(OrderStatus.DELIVERED):
            return Err(self._refused(OrderStatus.DELIVERED))
        self._move(OrderStatus.DELIVERED, actor, now)
        return Ok(None)

    def cancel_unpaid(self, reason: str, actor: str, now: datetime) -> Result[None, DomainError]:
        if self.status is not OrderStatus.PENDING_PAYMENT:
            return Err(self._refused(OrderStatus.CANCELLED))
        match _text(reason, REASON_MAX, "reason"):
            case Err(invalid):
                return Err(invalid)
            case Ok(clean):
                pass
        self.cancel_reason = clean
        self._move(OrderStatus.CANCELLED, actor, now, note=clean)
        return Ok(None)

    def cancel_paid(self, reason: str, actor: str, now: datetime) -> Result[None, DomainError]:
        """Only a paid order, not yet prepared or shipped. The refund stays pending until
        someone marks it done (decision 36)."""
        if self.status is not OrderStatus.PAID:
            return Err(self._refused(OrderStatus.CANCELLED))
        match _text(reason, REASON_MAX, "reason"):
            case Err(invalid):
                return Err(invalid)
            case Ok(clean):
                pass
        self.cancel_reason = clean
        self.refund_status = RefundStatus.PENDING
        self._move(OrderStatus.CANCELLED, actor, now, note=clean)
        return Ok(None)

    def mark_refunded(self, note: str, actor: str, now: datetime) -> Result[None, DomainError]:
        """The refund of a cancelled paid order was made by hand; the history row keeps the
        status cancelled to cancelled with the note."""
        if (
            self.status is not OrderStatus.CANCELLED
            or self.refund_status is not RefundStatus.PENDING
        ):
            return Err(self._refused(OrderStatus.CANCELLED))
        match _text(note, NOTE_MAX, "note"):
            case Err(invalid):
                return Err(invalid)
            case Ok(clean):
                pass
        self.refund_status = RefundStatus.DONE
        self._move(OrderStatus.CANCELLED, actor, now, note=clean)
        return Ok(None)

    def _can_move_to(self, to: OrderStatus) -> bool:
        return to in ALLOWED_TRANSITIONS[self.status]

    def _refused(self, to: OrderStatus) -> OrderTransitionInvalid:
        return OrderTransitionInvalid(details={"from": self.status.value, "to": to.value})

    def _move(
        self,
        to: OrderStatus,
        actor: str,
        now: datetime,
        *,
        note: str | None = None,
        carrier: str | None = None,
        tracking_number: str | None = None,
    ) -> None:
        previous = self.status
        self.status = to
        self.updated_at = now
        self.new_history.append(
            OrderEvent(
                at=now,
                from_status=previous,
                to_status=to,
                actor=actor,
                note=note,
                carrier=carrier,
                tracking_number=tracking_number,
            )
        )
        self.record(
            OrderStatusChanged(
                order_id=self.id,
                number=self.number,
                from_status=previous.value,
                to_status=to.value,
            )
        )
