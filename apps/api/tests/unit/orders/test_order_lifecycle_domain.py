"""Plan 004 step 3, domain: the order state machine (the full 7x7 matrix), the rules of every
move, the bounds of notes, reasons and shipping data, the refund flag, and one history row plus
one `orders.order.status_changed` event per move."""

from datetime import timedelta
from itertools import product
from uuid import UUID

import pytest

from fragancia_api.modules.orders.domain.customer import (
    Address,
    CustomerEmail,
    CustomerName,
    Phone,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod, Pickup
from fragancia_api.modules.orders.domain.errors import OrderDataInvalid, OrderTransitionInvalid
from fragancia_api.modules.orders.domain.order import (
    ALLOWED_TRANSITIONS,
    AUTO_CANCEL_REASON,
    SYSTEM_ACTOR,
    Delivery,
    Order,
    OrderEvent,
    OrderLine,
    OrderStatus,
    OrderStatusChanged,
    RefundStatus,
    clean_note_text,
    parse_order_number,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW, presentation_id

LATER = NOW + timedelta(hours=1)
ADMIN = "admin-1"

S = OrderStatus
# The table of the plan ("The Order aggregate gains the full state machine"), written out
# independently of `ALLOWED_TRANSITIONS`.
EXPECTED: dict[OrderStatus, set[OrderStatus]] = {
    S.PENDING_PAYMENT: {S.PAID, S.CANCELLED},
    S.PAID: {S.PREPARING, S.SHIPPED, S.READY_FOR_PICKUP, S.CANCELLED},
    S.PREPARING: {S.SHIPPED, S.READY_FOR_PICKUP},
    S.SHIPPED: {S.DELIVERED},
    S.READY_FOR_PICKUP: {S.DELIVERED},
    S.DELIVERED: set(),
    S.CANCELLED: set(),
}


def address() -> Address:
    result = Address.create(
        street="Av. Reforma",
        exterior_number="100",
        interior_number=None,
        neighborhood="Juárez",
        postal_code="77500",
        municipality="Cancún",
        state="CMX",
        references=None,
    )
    assert isinstance(result, Ok), result
    return result.value


def make_order(
    method: DeliveryMethod = DeliveryMethod.HOME,
    status: OrderStatus = OrderStatus.PENDING_PAYMENT,
    *,
    reservation: bool = True,
) -> Order:
    if method == DeliveryMethod.PICKUP:
        pickup = Pickup(enabled=True, address="Calle 1", hours="10-18")
        built = Delivery.create(method, 0, None, pickup)
    else:
        built = Delivery.create(method, 9000, address(), None)
    assert isinstance(built, Ok), built
    name = CustomerName.create("Ana López")
    email = CustomerEmail.create("ana@example.com")
    phone = Phone.create("9981234567")
    assert isinstance(name, Ok) and isinstance(email, Ok) and isinstance(phone, Ok)
    order = Order.place(
        id=UUID(int=900),
        number=1001,
        access_token_digest="d" * 64,
        cart_token_digest="c" * 64,
        name=name.value,
        email=email.value,
        phone=phone.value,
        delivery=built.value,
        invoice=None,
        lines=[
            OrderLine.create(
                presentation_id=presentation_id(1),
                perfume_id=UUID(int=1001),
                label="Perfume 1",
                ml=100,
                availability="in_stock",
                lead_time_min_days=None,
                lead_time_max_days=None,
                quantity=1,
                unit_price_cents=100_000,
                regular_price_cents=100_000,
            )
        ],
        reservation_id=UUID(int=77) if reservation else None,
        payment_due_at=NOW + timedelta(minutes=30),
        now=NOW,
    )
    order.pull_events()
    order.status = status
    return order


def refused(result: Result[None, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


def ok(result: Result[None, DomainError]) -> None:
    assert isinstance(result, Ok), result


def attempt(order: Order, to: OrderStatus) -> list[Result[None, DomainError]]:
    """Every method that could take the order to `to`, called with valid data."""
    match to:
        case S.PAID:
            return [order.mark_paid("Transferencia", ADMIN, LATER)]
        case S.PREPARING:
            return [order.start_preparing(ADMIN, LATER)]
        case S.SHIPPED:
            carrier = "Estafeta" if order.delivery.method == DeliveryMethod.HOME else None
            tracking = "123" if order.delivery.method == DeliveryMethod.HOME else None
            return [order.ship(carrier, tracking, ADMIN, LATER)]
        case S.READY_FOR_PICKUP:
            return [order.ready_for_pickup(ADMIN, LATER)]
        case S.DELIVERED:
            return [order.deliver(ADMIN, LATER)]
        case S.CANCELLED:
            return [
                order.cancel_unpaid("Motivo", ADMIN, LATER),
                order.cancel_paid("Motivo", ADMIN, LATER),
            ]
    return []  # no move ever returns to pending_payment: the order is born there


# --- the matrix --------------------------------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), list(product(S, S)))
def test_the_transition_matrix_allows_exactly_the_plan_table(
    current: OrderStatus, target: OrderStatus
) -> None:
    method = DeliveryMethod.PICKUP if target is S.READY_FOR_PICKUP else DeliveryMethod.HOME
    order = make_order(method, current)

    results = attempt(order, target)

    if target in EXPECTED[current]:
        assert any(isinstance(r, Ok) for r in results)
        assert order.status is target
        assert len(order.new_history) == 1
    else:
        assert all(isinstance(r, Err) for r in results)
        assert all(
            isinstance(r.error, OrderTransitionInvalid) for r in results if isinstance(r, Err)
        )
        assert order.status is current
        assert order.new_history == []
        assert order.pull_events() == []


def test_the_module_transition_table_matches_the_plan_table() -> None:
    assert {k: set(v) for k, v in ALLOWED_TRANSITIONS.items()} == EXPECTED


def test_a_refused_transition_reports_from_and_to() -> None:
    order = make_order(status=S.DELIVERED)

    error = refused(order.start_preparing(ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert error.details == {"from": "delivered", "to": "preparing"}


# --- mark_paid ---------------------------------------------------------------------------------


def test_mark_paid_moves_to_paid_with_a_history_row_and_an_event() -> None:
    order = make_order()

    ok(order.mark_paid("  Transferencia   directa ", ADMIN, LATER))

    assert order.status is S.PAID
    assert order.updated_at == LATER
    assert order.new_history == [
        OrderEvent(
            at=LATER,
            from_status=S.PENDING_PAYMENT,
            to_status=S.PAID,
            actor=ADMIN,
            note="Transferencia directa",
        )
    ]
    events = order.pull_events()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, OrderStatusChanged)
    assert event.name == "orders.order.status_changed"
    assert (event.order_id, event.number) == (UUID(int=900), 1001)
    assert (event.from_status, event.to_status) == ("pending_payment", "paid")


@pytest.mark.parametrize("note", ["", "   ", " \n\t "])
def test_mark_paid_requires_a_note(note: str) -> None:
    order = make_order()

    error = refused(order.mark_paid(note, ADMIN, LATER))

    assert isinstance(error, OrderDataInvalid)
    assert (error.details or {})["field"] == "note"
    assert order.status is S.PENDING_PAYMENT
    assert order.new_history == []
    assert order.pull_events() == []


def test_mark_paid_accepts_300_characters_and_refuses_301() -> None:
    assert isinstance(make_order().mark_paid("n" * 300, ADMIN, LATER), Ok)

    error = refused(make_order().mark_paid("n" * 301, ADMIN, LATER))

    assert isinstance(error, OrderDataInvalid)
    assert (error.details or {})["field"] == "note"


def test_mark_paid_counts_the_note_after_collapsing_whitespace() -> None:
    padded = "a" + " " * 400 + "b"

    assert isinstance(make_order().mark_paid(padded, ADMIN, LATER), Ok)


def test_the_transition_is_checked_before_the_note() -> None:
    order = make_order(status=S.PAID)

    error = refused(order.mark_paid("", ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)


# --- start_preparing / deliver -----------------------------------------------------------------


def test_start_preparing_records_the_move() -> None:
    order = make_order(status=S.PAID)

    ok(order.start_preparing(ADMIN, LATER))

    assert order.status is S.PREPARING
    assert [(e.from_status, e.to_status, e.actor) for e in order.new_history] == [
        (S.PAID, S.PREPARING, ADMIN)
    ]
    assert len(order.pull_events()) == 1


@pytest.mark.parametrize("start", [S.SHIPPED, S.READY_FOR_PICKUP])
def test_deliver_works_from_shipped_and_ready_for_pickup(start: OrderStatus) -> None:
    order = make_order(status=start)

    ok(order.deliver(ADMIN, LATER))

    assert order.status is S.DELIVERED


# --- ship --------------------------------------------------------------------------------------


@pytest.mark.parametrize("start", [S.PAID, S.PREPARING])
def test_a_home_order_ships_with_carrier_and_tracking(start: OrderStatus) -> None:
    order = make_order(DeliveryMethod.HOME, start)

    ok(order.ship(" Estafeta ", "123  456", ADMIN, LATER))

    assert order.status is S.SHIPPED
    assert (order.shipping_carrier, order.tracking_number) == ("Estafeta", "123 456")
    assert order.new_history == [
        OrderEvent(
            at=LATER,
            from_status=start,
            to_status=S.SHIPPED,
            actor=ADMIN,
            carrier="Estafeta",
            tracking_number="123 456",
        )
    ]


@pytest.mark.parametrize(
    ("carrier", "tracking", "field"),
    [
        (None, "123", "carrier"),
        ("", "123", "carrier"),
        ("  ", "123", "carrier"),
        ("E" * 61, "123", "carrier"),
        ("Estafeta", None, "tracking_number"),
        ("Estafeta", " ", "tracking_number"),
        ("Estafeta", "1" * 61, "tracking_number"),
    ],
)
def test_a_home_order_refuses_missing_or_oversized_shipping_data(
    carrier: str | None, tracking: str | None, field: str
) -> None:
    order = make_order(DeliveryMethod.HOME, S.PAID)

    error = refused(order.ship(carrier, tracking, ADMIN, LATER))

    assert isinstance(error, OrderDataInvalid)
    assert (error.details or {})["field"] == field
    assert order.status is S.PAID
    assert (order.shipping_carrier, order.tracking_number) == (None, None)
    assert order.new_history == []


def test_a_home_order_accepts_60_characters_for_carrier_and_tracking() -> None:
    order = make_order(DeliveryMethod.HOME, S.PAID)

    assert isinstance(order.ship("E" * 60, "1" * 60, ADMIN, LATER), Ok)


def test_a_local_order_ships_without_carrier_or_tracking() -> None:
    order = make_order(DeliveryMethod.LOCAL, S.PAID)

    ok(order.ship(None, None, ADMIN, LATER))

    assert order.status is S.SHIPPED
    assert (order.shipping_carrier, order.tracking_number) == (None, None)
    assert (order.new_history[0].carrier, order.new_history[0].tracking_number) == (None, None)


@pytest.mark.parametrize(("carrier", "tracking"), [("Estafeta", None), (None, "123"), ("E", "1")])
def test_a_local_order_refuses_a_carrier_or_tracking(
    carrier: str | None, tracking: str | None
) -> None:
    order = make_order(DeliveryMethod.LOCAL, S.PAID)

    error = refused(order.ship(carrier, tracking, ADMIN, LATER))

    assert isinstance(error, OrderDataInvalid)
    assert (error.details or {})["field"] == (
        "carrier" if carrier is not None else "tracking_number"
    )
    assert order.status is S.PAID


def test_a_pickup_order_is_never_shipped() -> None:
    order = make_order(DeliveryMethod.PICKUP, S.PAID)

    error = refused(order.ship(None, None, ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert error.details == {"from": "paid", "to": "shipped"}
    assert order.status is S.PAID


# --- ready_for_pickup --------------------------------------------------------------------------


@pytest.mark.parametrize("start", [S.PAID, S.PREPARING])
def test_a_pickup_order_becomes_ready_for_pickup(start: OrderStatus) -> None:
    order = make_order(DeliveryMethod.PICKUP, start)

    ok(order.ready_for_pickup(ADMIN, LATER))

    assert order.status is S.READY_FOR_PICKUP
    assert order.new_history[0].to_status is S.READY_FOR_PICKUP


@pytest.mark.parametrize("method", [DeliveryMethod.HOME, DeliveryMethod.LOCAL])
def test_only_a_pickup_order_becomes_ready_for_pickup(method: DeliveryMethod) -> None:
    order = make_order(method, S.PAID)

    error = refused(order.ready_for_pickup(ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert order.status is S.PAID


# --- cancel ------------------------------------------------------------------------------------


def test_cancel_unpaid_stores_the_reason_and_leaves_no_refund() -> None:
    order = make_order()

    ok(order.cancel_unpaid("  El cliente  lo pidió ", ADMIN, LATER))

    assert order.status is S.CANCELLED
    assert order.cancel_reason == "El cliente lo pidió"
    assert order.refund_status is None
    assert order.new_history == [
        OrderEvent(
            at=LATER,
            from_status=S.PENDING_PAYMENT,
            to_status=S.CANCELLED,
            actor=ADMIN,
            note="El cliente lo pidió",
        )
    ]
    event = order.pull_events()[0]
    assert isinstance(event, OrderStatusChanged)
    assert (event.from_status, event.to_status) == ("pending_payment", "cancelled")


def test_the_system_cancels_with_its_reason_and_actor() -> None:
    order = make_order()

    ok(order.cancel_unpaid(AUTO_CANCEL_REASON, SYSTEM_ACTOR, LATER))

    assert (AUTO_CANCEL_REASON, SYSTEM_ACTOR) == ("No pagado a tiempo", "system")
    assert order.cancel_reason == "No pagado a tiempo"
    assert order.new_history[0].actor == "system"


@pytest.mark.parametrize("cancel", ["cancel_unpaid", "cancel_paid"])
@pytest.mark.parametrize("reason", ["", "   "])
def test_a_cancellation_needs_a_reason(cancel: str, reason: str) -> None:
    start = S.PENDING_PAYMENT if cancel == "cancel_unpaid" else S.PAID
    order = make_order(status=start)

    error = refused(getattr(order, cancel)(reason, ADMIN, LATER))

    assert isinstance(error, OrderDataInvalid)
    assert (error.details or {})["field"] == "reason"
    assert order.status is start
    assert order.cancel_reason is None
    assert order.refund_status is None
    assert order.new_history == []


@pytest.mark.parametrize("cancel", ["cancel_unpaid", "cancel_paid"])
def test_a_reason_takes_300_characters_and_not_301(cancel: str) -> None:
    start = S.PENDING_PAYMENT if cancel == "cancel_unpaid" else S.PAID

    assert isinstance(getattr(make_order(status=start), cancel)("r" * 300, ADMIN, LATER), Ok)
    error = refused(getattr(make_order(status=start), cancel)("r" * 301, ADMIN, LATER))
    assert isinstance(error, OrderDataInvalid)


def test_cancel_unpaid_refuses_a_paid_order() -> None:
    order = make_order(status=S.PAID)

    error = refused(order.cancel_unpaid("x", ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert error.details == {"from": "paid", "to": "cancelled"}


def test_cancel_paid_leaves_the_refund_pending() -> None:
    order = make_order(status=S.PAID)

    ok(order.cancel_paid("Sin stock real", ADMIN, LATER))

    assert order.status is S.CANCELLED
    assert order.cancel_reason == "Sin stock real"
    assert order.refund_status is RefundStatus.PENDING
    assert order.new_history[0].from_status is S.PAID
    assert order.new_history[0].note == "Sin stock real"


@pytest.mark.parametrize(
    "start",
    [S.PENDING_PAYMENT, S.PREPARING, S.SHIPPED, S.READY_FOR_PICKUP, S.DELIVERED, S.CANCELLED],
)
def test_cancel_paid_only_applies_to_a_paid_order(start: OrderStatus) -> None:
    order = make_order(status=start)

    error = refused(order.cancel_paid("x", ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert order.refund_status is None
    assert order.status is start


# --- mark_refunded -----------------------------------------------------------------------------


def cancelled_paid() -> Order:
    order = make_order(status=S.PAID)
    ok(order.cancel_paid("Motivo", ADMIN, NOW))
    order.pull_events()
    order.new_history.clear()
    return order


def test_mark_refunded_closes_the_refund_and_keeps_the_status_cancelled() -> None:
    order = cancelled_paid()

    ok(order.mark_refunded(" Reembolso  por transferencia ", "owner-1", LATER))

    assert order.status is S.CANCELLED
    assert order.refund_status is RefundStatus.DONE
    assert order.updated_at == LATER
    assert order.new_history == [
        OrderEvent(
            at=LATER,
            from_status=S.CANCELLED,
            to_status=S.CANCELLED,
            actor="owner-1",
            note="Reembolso por transferencia",
        )
    ]
    event = order.pull_events()[0]
    assert isinstance(event, OrderStatusChanged)
    assert (event.from_status, event.to_status) == ("cancelled", "cancelled")


def test_a_second_refund_mark_is_refused() -> None:
    order = cancelled_paid()
    ok(order.mark_refunded("Hecho", ADMIN, LATER))
    order.new_history.clear()

    error = refused(order.mark_refunded("Otra vez", ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert order.refund_status is RefundStatus.DONE
    assert order.new_history == []


def test_an_unpaid_cancellation_has_no_refund_to_mark() -> None:
    order = make_order()
    ok(order.cancel_unpaid("Motivo", ADMIN, NOW))

    error = refused(order.mark_refunded("Hecho", ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)
    assert order.refund_status is None


@pytest.mark.parametrize("start", [S.PENDING_PAYMENT, S.PAID, S.SHIPPED, S.DELIVERED])
def test_a_refund_mark_needs_a_cancelled_order(start: OrderStatus) -> None:
    order = make_order(status=start)
    order.refund_status = RefundStatus.PENDING

    error = refused(order.mark_refunded("Hecho", ADMIN, LATER))

    assert isinstance(error, OrderTransitionInvalid)


@pytest.mark.parametrize("note", ["", "  ", "n" * 301])
def test_a_refund_note_is_required_and_bounded(note: str) -> None:
    order = cancelled_paid()

    error = refused(order.mark_refunded(note, ADMIN, LATER))

    assert isinstance(error, OrderDataInvalid)
    assert (error.details or {})["field"] == "note"
    assert order.refund_status is RefundStatus.PENDING


# --- the whole walk ----------------------------------------------------------------------------


def test_a_full_home_walk_appends_one_row_and_one_event_per_move() -> None:
    order = make_order(DeliveryMethod.HOME)

    ok(order.mark_paid("Transferencia", ADMIN, LATER))
    ok(order.ship("Estafeta", "123", ADMIN, LATER + timedelta(hours=1)))
    ok(order.deliver(ADMIN, LATER + timedelta(hours=2)))

    assert [(e.from_status, e.to_status) for e in order.new_history] == [
        (S.PENDING_PAYMENT, S.PAID),
        (S.PAID, S.SHIPPED),
        (S.SHIPPED, S.DELIVERED),
    ]
    assert [(e.from_status, e.to_status) for e in order.pull_events()] == [  # type: ignore[attr-defined]
        ("pending_payment", "paid"),
        ("paid", "shipped"),
        ("shipped", "delivered"),
    ]


def test_a_pickup_walk_goes_through_preparing() -> None:
    order = make_order(DeliveryMethod.PICKUP)

    ok(order.mark_paid("Efectivo", ADMIN, LATER))
    ok(order.start_preparing(ADMIN, LATER))
    ok(order.ready_for_pickup(ADMIN, LATER))
    ok(order.deliver(ADMIN, LATER))

    assert order.status is S.DELIVERED
    assert len(order.new_history) == 4


def test_nothing_moves_backwards() -> None:
    order = make_order(status=S.DELIVERED)

    for result in attempt(order, S.PAID) + attempt(order, S.SHIPPED) + attempt(order, S.CANCELLED):
        assert isinstance(refused(result), OrderTransitionInvalid)
    assert order.status is S.DELIVERED


# --- helpers -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LFI-1001", 1001),
        ("lfi-1001", 1001),
        ("1001", 1001),
        ("0001", 1),
        ("123456789", 123456789),
        ("", None),
        ("LFI-", None),
        ("LFI1001", None),
        ("abc", None),
        ("-1001", None),
        ("10.5", None),
        (" 1001", None),
        ("1234567890", None),
        ("١٢٣", None),
        ("LFI-LFI-1", None),
    ],
)
def test_parse_order_number(raw: str, expected: int | None) -> None:
    assert parse_order_number(raw) == expected


def test_clean_note_text_trims_the_ends_and_keeps_line_breaks() -> None:
    result = clean_note_text("  línea 1\nlínea 2  \n")

    assert result == Ok("línea 1\nlínea 2")


@pytest.mark.parametrize("raw", ["", "  \n ", "x" * 1001])
def test_clean_note_text_refuses_empty_and_over_1000(raw: str) -> None:
    result = clean_note_text(raw)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_ORDER_DATA_INVALID"
    assert (result.error.details or {})["field"] == "text"


def test_clean_note_text_accepts_exactly_1000() -> None:
    assert isinstance(clean_note_text("x" * 1000), Ok)
