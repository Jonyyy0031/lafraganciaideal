"""Orders on PostgreSQL (`fragancia_test`), plan 003 step 4: the gapless counter, the order and its
lines as rows, the table constraints and cascade, `SqlCartRepository.delete`, and
`StockHoldsAdapter` against the real inventory."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fragancia_api.container import Container
from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.orders.domain.customer import (
    Address,
    CustomerEmail,
    CustomerName,
    Invoice,
    Phone,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod
from fragancia_api.modules.orders.domain.order import (
    Delivery,
    Order,
    OrderLine,
)
from fragancia_api.modules.orders.infrastructure.sql_cart_repository import SqlCartRepository
from fragancia_api.modules.orders.infrastructure.sql_order_repository import SqlOrderRepository
from fragancia_api.modules.orders.infrastructure.stock_holds import StockHoldsAdapter
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import (
    in_minutes,
    item_row,
    reservation_row,
    table_count,
)
from tests.integration.inventory.test_stock_status_flow import restock
from tests.integration.orders.test_sql_carts import count, in_transaction, line, make_cart, store

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TIMEOUT = 5


def unwrap[T](result: Result[T, Any]) -> T:
    assert isinstance(result, Ok), result
    return result.value


def order_line(
    n: int = 1,
    quantity: int = 2,
    *,
    availability: str = "in_stock",
    max_days: int | None = None,
) -> OrderLine:
    return OrderLine(
        presentation_id=UUID(int=n),
        perfume_id=UUID(int=1000 + n),
        label=f"Brand Perfume {n} EDT 100 ml",
        ml=100,
        availability=availability,  # type: ignore[arg-type]
        lead_time_min_days=None if max_days is None else max_days - 2,
        lead_time_max_days=max_days,
        quantity=quantity,
        unit_price_cents=250_000,
        regular_price_cents=300_000,
        line_total_cents=250_000 * quantity,
    )


def make_order(
    *,
    number: int = 1001,
    digest: str | None = None,
    pickup: bool = False,
    invoice: bool = False,
    lines: tuple[OrderLine, ...] | None = None,
    reservation_id: UUID | None = None,
) -> Order:
    order_id = new_id()
    address = None
    if not pickup:
        address = unwrap(
            Address.create(
                street="Av. Reforma",
                exterior_number="100",
                interior_number="4B",
                neighborhood="Juárez",
                postal_code="01000",
                municipality="Cuauhtémoc",
                state="CMX",
                references="Frente al parque",
            )
        )
    delivery = Delivery(
        method=DeliveryMethod.PICKUP if pickup else DeliveryMethod.HOME,
        cost_cents=0 if pickup else 18_000,
        address=address,
        pickup_address="Calle 1 #2, Cancun" if pickup else None,
        pickup_hours="Lun-Vie 10-18" if pickup else None,
    )
    the_invoice = (
        unwrap(
            Invoice.create(
                rfc="XAXX010101000",
                business_name="Ana López",
                tax_regime="601",
                cfdi_use="G03",
                postal_code="01000",
            )
        )
        if invoice
        else None
    )
    return Order.place(
        id=order_id,
        number=number,
        access_token_digest=digest or order_id.hex.ljust(64, "0"),
        name=unwrap(CustomerName.create("Ana López")),
        email=unwrap(CustomerEmail.create("ana@example.com")),
        phone=unwrap(Phone.create("9981234567")),
        delivery=delivery,
        invoice=the_invoice,
        lines=lines if lines is not None else (order_line(1), order_line(2, 1)),
        reservation_id=reservation_id,
        payment_due_at=T0 + timedelta(minutes=30),
        now=T0,
    )


async def store_order(container: Container, order: Order) -> None:
    repo = SqlOrderRepository(container.database)
    await in_transaction(container, lambda: repo.add(order))


async def row(container: Container, query: str, **params: object) -> dict[str, Any]:
    async with container.database.engine.connect() as connection:
        found = (await connection.execute(text(query), params)).mappings().one()
    return dict(found)


async def rows(container: Container, query: str, **params: object) -> list[dict[str, Any]]:
    async with container.database.engine.connect() as connection:
        found = (await connection.execute(text(query), params)).mappings().all()
    return [dict(r) for r in found]


async def last_number(container: Container) -> int:
    found = await row(container, "SELECT last_number FROM orders.order_numbers WHERE id = 1")
    return int(found["last_number"])


async def failing_insert(container: Container, statement: str, **params: object) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text(statement), params)


# --- the counter --------------------------------------------------------------------------------


async def test_the_migration_seeds_the_counter_so_the_first_number_is_1001(
    container: Container,
) -> None:
    repo = SqlOrderRepository(container.database)

    first = await in_transaction(container, repo.next_number)
    second = await in_transaction(container, repo.next_number)

    assert (first, second) == (1001, 1002)
    assert await last_number(container) == 1002


async def test_two_numbers_in_one_transaction_are_consecutive(container: Container) -> None:
    repo = SqlOrderRepository(container.database)

    async def two() -> tuple[int, int]:
        return await repo.next_number(), await repo.next_number()

    assert await in_transaction(container, two) == (1001, 1002)


async def test_a_rolled_back_transaction_gives_the_number_back(container: Container) -> None:
    repo = SqlOrderRepository(container.database)

    async def refused() -> Result[int, DomainError]:
        await repo.next_number()
        return Err(DomainError(code="TEST_REFUSED", message="refused"))

    result = await SqlTransactionRunner(container.database).run(refused)

    assert isinstance(result, Err)
    assert await last_number(container) == 1000
    assert await in_transaction(container, repo.next_number) == 1001


async def test_a_second_transaction_waits_for_the_counter_and_takes_the_next_number(
    container: Container,
) -> None:
    repo = SqlOrderRepository(container.database)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def first_work() -> int:
        number = await repo.next_number()
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)
        return number

    first = asyncio.create_task(in_transaction(container, first_work))
    await asyncio.wait_for(holding.wait(), TIMEOUT)
    second = asyncio.create_task(in_transaction(container, repo.next_number))
    await asyncio.sleep(0.5)
    waiting = not second.done()
    release.set()
    numbers = await asyncio.wait_for(asyncio.gather(first, second), TIMEOUT)

    assert waiting, "the second transaction must wait for the counter row"
    assert list(numbers) == [1001, 1002]


# --- the order as rows --------------------------------------------------------------------------


async def test_a_home_order_is_stored_with_every_column(container: Container) -> None:
    reservation = new_id()  # no foreign key to inventory: any id is accepted
    order = make_order(reservation_id=reservation)

    await store_order(container, order)

    stored = await row(container, "SELECT * FROM orders.orders")
    assert (stored["id"], stored["number"], stored["status"]) == (
        order.id,
        1001,
        "pending_payment",
    )
    assert stored["access_token_digest"] == order.access_token_digest
    assert (stored["customer_name"], stored["customer_email"], stored["customer_phone"]) == (
        "Ana López",
        "ana@example.com",
        "9981234567",
    )
    assert (stored["delivery_method"], stored["delivery_cents"]) == ("home", 18_000)
    assert (
        stored["address_street"],
        stored["address_exterior_number"],
        stored["address_interior_number"],
        stored["address_neighborhood"],
        stored["address_postal_code"],
        stored["address_municipality"],
        stored["address_state"],
        stored["address_references"],
    ) == (
        "Av. Reforma",
        "100",
        "4B",
        "Juárez",
        "01000",
        "Cuauhtémoc",
        "CMX",
        "Frente al parque",
    )
    assert (stored["pickup_address"], stored["pickup_hours"]) == (None, None)
    assert all(
        stored[key] is None
        for key in (
            "invoice_rfc",
            "invoice_business_name",
            "invoice_tax_regime",
            "invoice_cfdi_use",
            "invoice_postal_code",
        )
    )
    assert (stored["subtotal_cents"], stored["total_cents"]) == (750_000, 768_000)
    assert stored["lead_time_max_days"] is None
    assert stored["reservation_id"] == reservation
    assert stored["payment_due_at"] == T0 + timedelta(minutes=30)
    assert (stored["created_at"], stored["updated_at"]) == (T0, T0)


async def test_the_lines_are_stored_in_position_order_with_their_frozen_prices(
    container: Container,
) -> None:
    order = make_order(
        lines=(order_line(5, 2), order_line(3, 1, availability="made_to_order", max_days=10))
    )

    await store_order(container, order)

    stored = await rows(container, "SELECT * FROM orders.order_lines ORDER BY position")
    assert [(r["position"], r["presentation_id"]) for r in stored] == [
        (0, UUID(int=5)),
        (1, UUID(int=3)),
    ]
    first, second = stored
    assert (first["order_id"], first["perfume_id"]) == (order.id, UUID(int=1005))
    assert (first["label"], first["ml"], first["availability"]) == (
        "Brand Perfume 5 EDT 100 ml",
        100,
        "in_stock",
    )
    assert (first["lead_time_min_days"], first["lead_time_max_days"]) == (None, None)
    assert (
        first["quantity"],
        first["unit_price_cents"],
        first["regular_price_cents"],
        first["line_total_cents"],
    ) == (2, 250_000, 300_000, 500_000)
    assert (second["availability"], second["lead_time_min_days"], second["lead_time_max_days"]) == (
        "made_to_order",
        8,
        10,
    )


async def test_a_pickup_order_with_an_invoice_stores_the_pickup_point_and_no_address(
    container: Container,
) -> None:
    order = make_order(pickup=True, invoice=True)

    await store_order(container, order)

    stored = await row(container, "SELECT * FROM orders.orders")
    assert stored["delivery_method"] == "pickup"
    assert stored["delivery_cents"] == 0
    assert (stored["pickup_address"], stored["pickup_hours"]) == (
        "Calle 1 #2, Cancun",
        "Lun-Vie 10-18",
    )
    assert all(stored[key] is None for key in stored if key.startswith("address_"))
    assert (
        stored["invoice_rfc"],
        stored["invoice_business_name"],
        stored["invoice_tax_regime"],
        stored["invoice_cfdi_use"],
        stored["invoice_postal_code"],
    ) == ("XAXX010101000", "Ana López", "601", "G03", "01000")


async def test_an_order_without_a_reservation_stores_null(container: Container) -> None:
    await store_order(
        container, make_order(lines=(order_line(1, 1, availability="made_to_order", max_days=5),))
    )

    stored = await row(container, "SELECT reservation_id, lead_time_max_days FROM orders.orders")

    assert stored == {"reservation_id": None, "lead_time_max_days": 5}


async def test_a_rolled_back_add_stores_neither_the_order_nor_its_lines(
    container: Container,
) -> None:
    repo = SqlOrderRepository(container.database)
    order = make_order()

    async def refused() -> Result[None, DomainError]:
        await repo.add(order)
        return Err(DomainError(code="TEST_REFUSED", message="refused"))

    await SqlTransactionRunner(container.database).run(refused)

    assert await count(container, "orders") == 0
    assert await count(container, "order_lines") == 0


# --- constraints --------------------------------------------------------------------------------


async def test_the_order_number_is_unique(container: Container) -> None:
    await store_order(container, make_order(number=1001))

    with pytest.raises(IntegrityError):
        await store_order(container, make_order(number=1001))


async def test_the_access_token_digest_is_unique(container: Container) -> None:
    await store_order(container, make_order(number=1001, digest="a" * 64))

    with pytest.raises(IntegrityError):
        await store_order(container, make_order(number=1002, digest="a" * 64))


async def test_the_counter_table_rejects_a_second_row_with_the_same_id(
    container: Container,
) -> None:
    with pytest.raises(IntegrityError):
        await failing_insert(
            container, "INSERT INTO orders.order_numbers (id, last_number) VALUES (1, 5)"
        )


_ORDER_INSERT = (
    "INSERT INTO orders.orders (id, number, status, access_token_digest, customer_name, "
    "customer_email, customer_phone, delivery_method, delivery_cents, subtotal_cents, "
    "total_cents, payment_due_at, created_at, updated_at) VALUES (:id, :number, 'pending_payment', "
    ":digest, 'Ana', 'a@b.co', '9981234567', :method, :delivery, :subtotal, :total, now(), now(), "
    "now())"
)


def raw_order(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": new_id(),
        "number": 2001,
        "digest": "c" * 64,
        "method": "home",
        "delivery": 100,
        "subtotal": 1_000,
        "total": 1_100,
    }
    values.update(overrides)
    return values


async def test_the_total_must_equal_the_subtotal_plus_the_delivery(container: Container) -> None:
    await failing_insert(container, _ORDER_INSERT, **raw_order())

    with pytest.raises(IntegrityError):
        await failing_insert(
            container,
            _ORDER_INSERT,
            **raw_order(id=new_id(), number=2002, digest="d" * 64, total=1),
        )


@pytest.mark.parametrize(
    ("method", "accepted"), [("home", True), ("local", True), ("pickup", True), ("courier", False)]
)
async def test_the_delivery_method_is_one_of_the_three(
    container: Container, method: str, accepted: bool
) -> None:
    if accepted:
        await failing_insert(container, _ORDER_INSERT, **raw_order(method=method))
    else:
        with pytest.raises(IntegrityError):
            await failing_insert(container, _ORDER_INSERT, **raw_order(method=method))


async def insert_line(container: Container, order_id: UUID, **overrides: object) -> None:
    values: dict[str, object] = {
        "order_id": order_id,
        "position": 0,
        "quantity": 2,
        "unit": 100,
        "total": 200,
    }
    values.update(overrides)
    await failing_insert(
        container,
        "INSERT INTO orders.order_lines (order_id, position, presentation_id, perfume_id, label, "
        "ml, availability, quantity, unit_price_cents, regular_price_cents, line_total_cents) "
        "VALUES (:order_id, :position, gen_random_uuid(), gen_random_uuid(), 'x', 50, 'in_stock', "
        ":quantity, :unit, :unit, :total)",
        **values,
    )


@pytest.mark.parametrize(("quantity", "accepted"), [(0, False), (1, True), (10, True), (11, False)])
async def test_a_line_quantity_is_between_1_and_10(
    container: Container, quantity: int, accepted: bool
) -> None:
    order = make_order()
    await store_order(container, order)
    kwargs = {"position": 5, "quantity": quantity, "total": 100 * quantity}

    if accepted:
        await insert_line(container, order.id, **kwargs)
    else:
        with pytest.raises(IntegrityError):
            await insert_line(container, order.id, **kwargs)


async def test_a_line_total_must_be_the_unit_price_times_the_quantity(container: Container) -> None:
    order = make_order()
    await store_order(container, order)

    with pytest.raises(IntegrityError):
        await insert_line(container, order.id, position=5, quantity=2, unit=100, total=199)


async def test_a_position_appears_once_per_order(container: Container) -> None:
    order = make_order()
    await store_order(container, order)

    with pytest.raises(IntegrityError):
        await insert_line(container, order.id, position=0)


async def test_a_line_needs_an_existing_order(container: Container) -> None:
    with pytest.raises(IntegrityError):
        await insert_line(container, new_id())


async def test_deleting_an_order_deletes_its_lines_only(container: Container) -> None:
    kept, removed = make_order(number=1001), make_order(number=1002)
    await store_order(container, kept)
    await store_order(container, removed)
    assert await count(container, "order_lines") == 4

    async with container.database.engine.begin() as connection:
        proof = (
            await connection.execute(
                text("SELECT count(*) FROM orders.orders WHERE id = :id"), {"id": removed.id}
            )
        ).scalar_one()
        assert proof == 1
        await connection.execute(
            text("DELETE FROM orders.orders WHERE id = :id"), {"id": removed.id}
        )

    assert await count(container, "orders") == 1
    remaining = await rows(container, "SELECT DISTINCT order_id FROM orders.order_lines")
    assert [r["order_id"] for r in remaining] == [kept.id]


async def test_the_customer_email_is_indexed(container: Container) -> None:
    indexed = await rows(
        container,
        "SELECT a.attname AS name FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'orders.orders'::regclass AND NOT i.indisprimary "
        "AND NOT i.indisunique",
    )

    assert {r["name"] for r in indexed} == {"customer_email"}


# --- SqlCartRepository.delete -------------------------------------------------------------------


async def test_deleting_a_cart_removes_it_and_its_lines_and_leaves_the_others(
    container: Container,
) -> None:
    gone = make_cart("a" * 64, line(), line(minutes=1))
    kept = make_cart("b" * 64, line())
    await store(container, gone)
    await store(container, kept)
    repo = SqlCartRepository(container.database)

    await in_transaction(container, lambda: repo.delete(gone))

    assert await count(container, "carts") == 1
    assert await count(container, "cart_lines") == 1
    assert await in_transaction(container, lambda: repo.get_by_token_for_update("a" * 64)) is None
    assert (
        await in_transaction(container, lambda: repo.get_by_token_for_update("b" * 64)) is not None
    )


async def test_a_rolled_back_delete_keeps_the_cart(container: Container) -> None:
    cart = make_cart("c" * 64, line(), line(minutes=1))
    await store(container, cart)
    repo = SqlCartRepository(container.database)

    async def refused() -> Result[None, DomainError]:
        await repo.delete(cart)
        return Err(DomainError(code="TEST_REFUSED", message="refused"))

    await SqlTransactionRunner(container.database).run(refused)

    assert await count(container, "carts") == 1
    assert await count(container, "cart_lines") == 2


# --- StockHoldsAdapter against the real inventory ----------------------------------------------


def adapter(container: Container) -> StockHoldsAdapter:
    return StockHoldsAdapter(container.services.get(StockReservations))


async def stocked(container: Container, catalog_rows: CatalogRows, *units: int) -> list[UUID]:
    """One in-stock presentation per entry, with that many units on hand in inventory."""
    _, ids = await catalog_rows.perfume("Hold", sizes=tuple({"stock_available": u} for u in units))
    for presentation, quantity in zip(ids, units, strict=True):
        await restock(container, presentation, quantity)
    return ids


async def test_a_hold_reserves_the_units_under_the_reference_until_the_expiry(
    container: Container, catalog_rows: CatalogRows
) -> None:
    [first, second] = await stocked(container, catalog_rows, 5, 3)
    expires = in_minutes(30)

    result = await adapter(container).hold("order:abc", {first: 2, second: 3}, expires)

    reservation_id = unwrap(result)
    stored = await reservation_row(container, reservation_id)
    assert (stored["reference"], stored["status"]) == ("order:abc", "active")
    assert abs(stored["expires_at"] - expires) < timedelta(seconds=1)
    assert await item_row(container, first) == (5, 2)
    assert await item_row(container, second) == (3, 3)


async def test_a_hold_that_is_short_for_one_presentation_reserves_nothing_at_all(
    container: Container, catalog_rows: CatalogRows
) -> None:
    [enough, short] = await stocked(container, catalog_rows, 5, 1)

    result = await adapter(container).hold("order:abc", {enough: 2, short: 4}, in_minutes(30))

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert result.error.details == {
        "lines": [{"presentation_id": str(short), "requested": 4, "available": 1}]
    }
    assert await item_row(container, enough) == (5, 0)
    assert await item_row(container, short) == (1, 0)
    assert await table_count(container, "reservations") == 0
    assert await table_count(container, "reservation_lines") == 0


async def test_holding_the_same_reference_twice_is_refused_by_inventory_unchanged(
    container: Container, catalog_rows: CatalogRows
) -> None:
    [presentation] = await stocked(container, catalog_rows, 5)
    await adapter(container).hold("order:abc", {presentation: 1}, in_minutes(30))

    again = await adapter(container).hold("order:abc", {presentation: 1}, in_minutes(30))

    assert isinstance(again, Err)
    assert again.error.code == "INVENTORY_RESERVATION_REFERENCE_TAKEN"
    assert await item_row(container, presentation) == (5, 1)


async def test_a_made_to_order_presentation_is_refused_by_inventory_unchanged(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation] = await catalog_rows.perfume(
        "Order",
        sizes=(
            {"availability": "made_to_order", "lead_time_min_days": 3, "lead_time_max_days": 6},
        ),
    )

    result = await adapter(container).hold("order:abc", {presentation: 1}, in_minutes(30))

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"


async def test_a_hold_inside_the_callers_transaction_rolls_back_with_it(
    container: Container, catalog_rows: CatalogRows
) -> None:
    [presentation] = await stocked(container, catalog_rows, 5)
    holds = adapter(container)

    async def hold_then_refuse() -> Result[UUID, DomainError]:
        held = await holds.hold("order:abc", {presentation: 2}, in_minutes(30))
        assert isinstance(held, Ok)
        return Err(DomainError(code="TEST_REFUSED", message="refused"))

    await SqlTransactionRunner(container.database).run(hold_then_refuse)

    assert await item_row(container, presentation) == (5, 0)
    assert await table_count(container, "reservations") == 0
