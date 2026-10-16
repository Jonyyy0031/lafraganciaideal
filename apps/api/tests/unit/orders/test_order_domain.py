"""The order aggregate (plan 003 step 2): totals, lead time, status, the event and the number."""

from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.orders.domain.customer import (
    Address,
    CustomerEmail,
    CustomerName,
    Invoice,
    Phone,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod
from fragancia_api.modules.orders.domain.order import (
    FIRST_ORDER_NUMBER,
    ORDER_NUMBER_PREFIX,
    PAYMENT_WINDOW,
    Delivery,
    Order,
    OrderLine,
    OrderPlaced,
    OrderStatus,
    format_order_number,
)
from fragancia_api.shared.kernel import Ok, Result
from tests.unit.orders.support import NOW, presentation_id

ORDER_ID = UUID(int=900)


def unwrap[T](result: Result[T, object]) -> T:
    assert isinstance(result, Ok), result
    return result.value


def line(
    n: int,
    quantity: int = 1,
    *,
    price: int = 100_000,
    availability: str = "in_stock",
    max_days: int | None = None,
) -> OrderLine:
    return OrderLine(
        presentation_id=presentation_id(n),
        perfume_id=UUID(int=1000 + n),
        label=f"Perfume {n}",
        ml=100,
        availability=availability,  # type: ignore[arg-type]
        lead_time_min_days=None if max_days is None else max_days - 1,
        lead_time_max_days=max_days,
        quantity=quantity,
        unit_price_cents=price,
        regular_price_cents=price + 10_000,
        line_total_cents=price * quantity,
    )


def delivery(cost: int = 0) -> Delivery:
    return Delivery(
        method=DeliveryMethod.PICKUP,
        cost_cents=cost,
        address=None,
        pickup_address="Calle 1",
        pickup_hours="10-18",
    )


def place(*lines: OrderLine, cost: int = 0, invoice: Invoice | None = None) -> Order:
    return Order.place(
        id=ORDER_ID,
        number=1001,
        access_token_digest="d" * 64,
        name=unwrap(CustomerName.create("Ana López")),
        email=unwrap(CustomerEmail.create("ana@example.com")),
        phone=unwrap(Phone.create("9981234567")),
        delivery=delivery(cost),
        invoice=invoice,
        lines=lines,
        reservation_id=UUID(int=77),
        payment_due_at=NOW + PAYMENT_WINDOW,
        now=NOW,
    )


def test_the_numbering_constants_follow_the_decisions() -> None:
    assert ORDER_NUMBER_PREFIX == "LFI-"
    assert FIRST_ORDER_NUMBER == 1001
    assert PAYMENT_WINDOW == timedelta(minutes=30)


def test_an_order_number_is_the_prefix_and_the_integer() -> None:
    assert format_order_number(1001) == "LFI-1001"
    assert format_order_number(12345) == "LFI-12345"


def test_the_only_status_is_pending_payment() -> None:
    assert [status.value for status in OrderStatus] == ["pending_payment"]


def test_placing_an_order_leaves_it_pending_payment_with_its_stamps() -> None:
    order = place(line(1))

    assert order.status is OrderStatus.PENDING_PAYMENT
    assert (order.id, order.number, order.access_token_digest) == (ORDER_ID, 1001, "d" * 64)
    assert (order.created_at, order.updated_at) == (NOW, NOW)
    assert order.payment_due_at == NOW + timedelta(minutes=30)
    assert order.reservation_id == UUID(int=77)


def test_the_subtotal_is_the_sum_of_the_line_totals_and_the_total_adds_delivery() -> None:
    order = place(line(1, 2, price=250_000), line(2, 3, price=100_000), cost=18_000)

    assert order.subtotal_cents == 800_000
    assert order.delivery.cost_cents == 18_000
    assert order.total_cents == 818_000


def test_a_free_delivery_total_equals_the_subtotal() -> None:
    order = place(line(1, 1, price=300_000))

    assert order.total_cents == order.subtotal_cents == 300_000


def test_the_lead_time_is_the_longest_among_made_to_order_lines() -> None:
    order = place(
        line(1),
        line(2, availability="made_to_order", max_days=5),
        line(3, availability="made_to_order", max_days=10),
    )

    assert order.lead_time_max_days == 10


def test_an_in_stock_lead_time_is_ignored() -> None:
    order = place(line(1, max_days=20), line(2, availability="made_to_order", max_days=4))

    assert order.lead_time_max_days == 4


def test_the_lead_time_is_none_when_every_line_is_in_stock() -> None:
    assert place(line(1), line(2, max_days=9)).lead_time_max_days is None


def test_the_lead_time_is_none_for_a_made_to_order_line_without_one() -> None:
    assert place(line(1, availability="made_to_order")).lead_time_max_days is None


def test_the_lines_keep_their_frozen_prices_and_order() -> None:
    first, second = line(1, price=111_000), line(2, price=222_000)

    order = place(first, second)

    assert order.lines == [first, second]
    assert (order.lines[0].unit_price_cents, order.lines[0].regular_price_cents) == (
        111_000,
        121_000,
    )


def test_placing_records_one_order_placed_event() -> None:
    order = place(line(1))

    [event] = order.pull_events()

    assert isinstance(event, OrderPlaced)
    assert event.name == "orders.order.placed"
    assert (event.order_id, event.number) == (ORDER_ID, 1001)
    assert order.pull_events() == []


def test_the_invoice_and_the_delivery_address_are_kept_as_given() -> None:
    invoice = unwrap(
        Invoice.create(
            rfc="XAXX010101000",
            business_name="Ana López",
            tax_regime="601",
            cfdi_use="G03",
            postal_code="01000",
        )
    )
    address = unwrap(
        Address.create(
            street="Reforma",
            exterior_number="1",
            interior_number=None,
            neighborhood="Centro",
            postal_code="01000",
            municipality="Cuauhtémoc",
            state="CMX",
            references=None,
        )
    )
    home = Delivery(DeliveryMethod.HOME, 18_000, address, None, None)

    order = Order.place(
        id=ORDER_ID,
        number=1002,
        access_token_digest="e" * 64,
        name=unwrap(CustomerName.create("Ana López")),
        email=unwrap(CustomerEmail.create("ana@example.com")),
        phone=unwrap(Phone.create("9981234567")),
        delivery=home,
        invoice=invoice,
        lines=[line(1)],
        reservation_id=None,
        payment_due_at=NOW + PAYMENT_WINDOW,
        now=NOW,
    )

    assert order.invoice == invoice
    assert order.delivery.address == address
    assert order.reservation_id is None
