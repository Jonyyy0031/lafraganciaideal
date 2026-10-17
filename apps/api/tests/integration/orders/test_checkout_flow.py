"""`PlaceOrder` over the real adapters and the real catalog and inventory facades on PostgreSQL
(`fragancia_test`), plan 003: the acceptance flow as rows, the refusals that must leave nothing
behind, the rollback of a failure after the stock hold, and concurrent checkouts."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationOffers
from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.orders.application.commands.cart import SetCartLine
from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.contracts import CheckoutInvoice, PlacedOrder
from fragancia_api.modules.orders.domain.order import Order
from fragancia_api.modules.orders.infrastructure.cart_tokens import SecureCartTokens
from fragancia_api.modules.orders.infrastructure.catalog_offers import CatalogOffersAdapter
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.modules.orders.infrastructure.sql_cart_repository import (
    SqlCartReader,
    SqlCartRepository,
)
from fragancia_api.modules.orders.infrastructure.sql_delivery_settings_repository import (
    SqlDeliverySettingsReader,
)
from fragancia_api.modules.orders.infrastructure.sql_order_repository import (
    SqlOrderReader,
    SqlOrderRepository,
)
from fragancia_api.modules.orders.infrastructure.stock_holds import StockHoldsAdapter
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import item_row, table_count
from tests.integration.inventory.test_stock_status_flow import lose, outbox_rows, restock
from tests.integration.orders.test_sql_carts import count
from tests.unit.orders.test_checkout import contact, request

pytestmark = pytest.mark.integration

TIMEOUT = 20
MADE_TO_ORDER = {
    "availability": "made_to_order",
    "lead_time_min_days": 8,
    "lead_time_max_days": 10,
    "price_cents": 100_000,
}


async def configure_delivery(container: Container) -> None:
    """Home 180.00 (never free), local 90.00 for 77500, pickup on."""
    async with container.database.engine.begin() as connection:
        done = await connection.execute(
            text(
                "UPDATE orders.delivery_settings SET home_enabled = true, "
                "home_price_cents = 18000, "
                "home_free_from_cents = NULL, local_enabled = true, local_price_cents = 9000, "
                "local_postal_codes = ARRAY['77500']::varchar(5)[], pickup_enabled = true, "
                "pickup_address = 'Calle 1 #2, Cancun', pickup_hours = 'Lun-Vie 10-18' "
                "WHERE id = 1"
            )
        )
        assert done.rowcount == 1


async def add_to_cart(
    container: Container, lines: list[tuple[UUID, int]], token: str | None = None
) -> str:
    set_line = container.services.get(SetCartLine)
    for presentation, quantity in lines:
        result = await set_line.execute(token, presentation, quantity)
        assert isinstance(result, Ok), result
        token = result.value.token
    assert token is not None
    return token


def real_place_order(container: Container, orders: Any = None) -> PlaceOrder:
    """`PlaceOrder` over the real adapters; `orders` may replace the order repository."""
    database = container.database
    carts = SqlCartRepository(database)
    links = HmacOrderLinks("integration-order-link-secret-0123456789")
    return PlaceOrder(
        carts=carts,
        cart_reader=SqlCartReader(database),
        cart_tokens=SecureCartTokens(),
        links=links,
        order_reader=SqlOrderReader(database),
        offers=CatalogOffersAdapter(container.services.get(PresentationOffers)),
        delivery_settings=SqlDeliverySettingsReader(database),
        orders=orders or SqlOrderRepository(database),
        stocks=StockHoldsAdapter(container.services.get(StockReservations)),
        transactions=SqlTransactionRunner(database),
        events=OutboxEventPublisher(database),
        clock=SystemClock(),
    )


async def rows(container: Container, query: str, **params: object) -> list[dict[str, Any]]:
    async with container.database.engine.connect() as connection:
        return [dict(r) for r in (await connection.execute(text(query), params)).mappings().all()]


async def last_number(container: Container) -> int:
    [found] = await rows(container, "SELECT last_number FROM orders.order_numbers WHERE id = 1")
    return int(found["last_number"])


def placed(result: Result[PlacedOrder, DomainError]) -> PlacedOrder:
    assert isinstance(result, Ok), result
    return result.value


def refusal(result: Result[PlacedOrder, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


async def order_placed_rows(container: Container) -> list[dict[str, Any]]:
    return [r for r in await outbox_rows(container) if r["name"] == "orders.order.placed"]


async def assert_no_order_traces(container: Container) -> None:
    assert await count(container, "orders") == 0
    assert await count(container, "order_lines") == 0
    assert await last_number(container) == 1000
    assert await table_count(container, "reservations") == 0
    assert await order_placed_rows(container) == []


# --- the happy path -----------------------------------------------------------------------------


async def test_a_home_checkout_stores_the_order_reserves_the_stock_and_empties_the_cart(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock, made] = await catalog_rows.perfume(
        "Flow", sizes=({"stock_available": 5}, MADE_TO_ORDER)
    )
    await restock(container, stock, 5)
    token = await add_to_cart(container, [(stock, 2), (made, 1)])
    before = datetime.now(UTC)

    result = placed(await real_place_order(container).execute(token, request("home", 618_000)))

    assert result.number == "LFI-1001"
    assert result.status == "pending_payment"
    assert (result.subtotal_cents, result.delivery_cents, result.total_cents) == (
        600_000,
        18_000,
        618_000,
    )
    assert result.lead_time_max_days == 10
    assert timedelta(minutes=29) < result.payment_due_at - before < timedelta(minutes=31)
    assert result.access_token not in token
    assert len(result.access_token) >= 40

    [order] = await rows(container, "SELECT * FROM orders.orders")
    assert (order["number"], order["status"]) == (1001, "pending_payment")
    assert order["access_token_digest"] != result.access_token
    assert len(order["access_token_digest"]) == 64
    assert (order["customer_name"], order["customer_email"], order["customer_phone"]) == (
        "Ana López",
        "ana@example.com",
        "9981234567",
    )
    assert (order["delivery_method"], order["delivery_cents"], order["address_state"]) == (
        "home",
        18_000,
        "CMX",
    )
    assert (order["subtotal_cents"], order["total_cents"], order["lead_time_max_days"]) == (
        600_000,
        618_000,
        10,
    )
    lines = await rows(container, "SELECT * FROM orders.order_lines ORDER BY position")
    assert [
        (r["presentation_id"], r["quantity"], r["unit_price_cents"], r["line_total_cents"])
        for r in lines
    ] == [(stock, 2, 250_000, 500_000), (made, 1, 100_000, 100_000)]
    assert [r["availability"] for r in lines] == ["in_stock", "made_to_order"]

    assert await count(container, "carts") == 0
    assert await count(container, "cart_lines") == 0

    [reservation] = await rows(container, "SELECT * FROM inventory.reservations")
    assert reservation["reference"] == f"order:{order['id']}"
    assert reservation["status"] == "active"
    assert order["reservation_id"] == reservation["id"]
    assert abs(reservation["expires_at"] - order["payment_due_at"]) < timedelta(seconds=1)
    reserved_lines = await rows(container, "SELECT * FROM inventory.reservation_lines")
    assert [(r["presentation_id"], r["quantity"]) for r in reserved_lines] == [(stock, 2)]
    assert await item_row(container, stock) == (5, 2)

    [event] = await order_placed_rows(container)
    assert event["payload"] == {"order_id": str(order["id"]), "number": 1001}


async def test_the_second_order_takes_the_next_number(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Two", sizes=({"stock_available": 9},))
    await restock(container, stock, 9)
    first_token = await add_to_cart(container, [(stock, 1)])
    second_token = await add_to_cart(container, [(stock, 1)])
    use_case = real_place_order(container)

    first = placed(await use_case.execute(first_token, request("home", 268_000)))
    second = placed(await use_case.execute(second_token, request("home", 268_000)))

    assert (first.number, second.number) == ("LFI-1001", "LFI-1002")
    assert first.access_token != second.access_token
    assert await last_number(container) == 1002
    assert await item_row(container, stock) == (9, 2)


async def test_a_retry_after_a_lost_response_gets_the_same_order_and_link(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Retry", sizes=({"stock_available": 9},))
    await restock(container, stock, 9)
    token = await add_to_cart(container, [(stock, 2)])
    use_case = real_place_order(container)

    first = placed(await use_case.execute(token, request("home", 518_000)))
    again = placed(await use_case.execute(token, request("home", 1)))  # body ignored

    assert again == first
    assert first.access_token == HmacOrderLinks("integration-order-link-secret-0123456789").token(
        (await rows(container, "SELECT id FROM orders.orders"))[0]["id"],
    )
    assert await count(container, "orders") == 1
    assert await table_count(container, "reservations") == 1
    assert await item_row(container, stock) == (9, 2)
    assert await last_number(container) == 1001
    assert len(await order_placed_rows(container)) == 1


class PausingOrders(SqlOrderRepository):
    """Holds the first checkout inside its transaction, after it locked the cart and held the
    stock, until the test lets it go: the retry then arrives while the cart row is locked."""

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.inside = asyncio.Event()
        self.release = asyncio.Event()

    async def add(self, order: Order) -> None:
        self.inside.set()
        await self.release.wait()
        await super().add(order)


async def wait_until_a_session_waits_on_a_lock(container: Container) -> None:
    for _ in range(100):
        waiting = await rows(
            container,
            "SELECT 1 FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'",
        )
        if waiting:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("no session ever waited on a lock")


async def test_a_retry_that_waits_on_the_cart_lock_behind_the_first_checkout_gets_its_order(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Race", sizes=({"stock_available": 9},))
    await restock(container, stock, 9)
    token = await add_to_cart(container, [(stock, 2)])
    pausing = PausingOrders(container.database)
    first_request = asyncio.create_task(
        real_place_order(container, pausing).execute(token, request("home", 518_000))
    )
    await asyncio.wait_for(pausing.inside.wait(), TIMEOUT)  # the first one holds the cart lock
    retry = asyncio.create_task(
        real_place_order(container).execute(token, request("home", 518_000))
    )
    await asyncio.wait_for(wait_until_a_session_waits_on_a_lock(container), TIMEOUT)
    assert not retry.done()  # the retry read the cart and now waits for the row lock

    pausing.release.set()
    first, second = await asyncio.wait_for(asyncio.gather(first_request, retry), TIMEOUT)

    assert placed(first).number == "LFI-1001"
    assert placed(second) == placed(first)  # the replay, not ORDERS_CHECKOUT_CART_CHANGED
    assert await count(container, "orders") == 1
    assert await count(container, "carts") == 0
    assert await table_count(container, "reservations") == 1
    assert await item_row(container, stock) == (9, 2)
    assert await last_number(container) == 1001
    assert len(await order_placed_rows(container)) == 1


async def test_no_replay_after_the_payment_window_or_for_another_cart(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("NoRetry", sizes=({"stock_available": 9},))
    await restock(container, stock, 9)
    token = await add_to_cart(container, [(stock, 1)])
    use_case = real_place_order(container)
    placed(await use_case.execute(token, request("home", 268_000)))

    other = refusal(await use_case.execute("another-cookie", request("home", 268_000)))
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE orders.orders SET created_at = now() - interval '31 minutes'")
        )
    late = refusal(await use_case.execute(token, request("home", 268_000)))

    assert other.code == late.code == "ORDERS_CHECKOUT_CART_EMPTY"
    assert await count(container, "orders") == 1


async def test_a_cart_of_only_made_to_order_lines_creates_no_reservation(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [made] = await catalog_rows.perfume("Made", sizes=(MADE_TO_ORDER,))
    token = await add_to_cart(container, [(made, 1)])

    result = placed(await real_place_order(container).execute(token, request("home", 118_000)))

    assert result.number == "LFI-1001"
    [order] = await rows(container, "SELECT reservation_id, lead_time_max_days FROM orders.orders")
    assert order == {"reservation_id": None, "lead_time_max_days": 10}
    assert await table_count(container, "reservations") == 0
    assert await table_count(container, "stock_items") == 0
    assert await count(container, "carts") == 0


async def test_a_pickup_order_with_an_invoice_stores_no_address_and_a_free_delivery(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Pick", sizes=({"stock_available": 5},))
    await restock(container, stock, 5)
    token = await add_to_cart(container, [(stock, 1)])
    invoice = CheckoutInvoice(
        rfc="xaxx010101000",
        business_name="Ana López",
        tax_regime="601",
        cfdi_use="G03",
        postal_code="01000",
    )

    result = placed(
        await real_place_order(container).execute(
            token, request("pickup", 250_000, invoice=invoice)
        )
    )

    assert result.delivery_cents == 0
    [order] = await rows(container, "SELECT * FROM orders.orders")
    assert order["delivery_cents"] == 0
    assert (order["pickup_address"], order["pickup_hours"]) == (
        "Calle 1 #2, Cancun",
        "Lun-Vie 10-18",
    )
    assert all(order[key] is None for key in order if key.startswith("address_"))
    assert (
        order["invoice_rfc"],
        order["invoice_business_name"],
        order["invoice_tax_regime"],
        order["invoice_cfdi_use"],
        order["invoice_postal_code"],
    ) == ("XAXX010101000", "Ana López", "601", "G03", "01000")


async def test_the_order_keeps_the_price_it_was_bought_at_after_the_catalog_changes(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Frozen", sizes=({"stock_available": 5},))
    await restock(container, stock, 5)
    token = await add_to_cart(container, [(stock, 1)])
    await real_place_order(container).execute(token, request("home", 268_000))

    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.presentations SET price_cents = 999 WHERE id = :id"),
            {"id": stock},
        )

    [line] = await rows(
        container, "SELECT unit_price_cents, line_total_cents FROM orders.order_lines"
    )
    assert line == {"unit_price_cents": 250_000, "line_total_cents": 250_000}


# --- refusals leave nothing behind --------------------------------------------------------------


async def test_a_stock_refusal_leaves_the_cart_and_consumes_no_number(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Short", sizes=({"stock_available": 5},))
    await restock(container, stock, 5)
    token = await add_to_cart(container, [(stock, 3)])
    await lose(container, stock, 4)  # the catalog copy still says 5; inventory has 1
    outbox_before = len(await outbox_rows(container))

    error = refusal(await real_place_order(container).execute(token, request("home", 768_000)))

    assert error.code == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert error.details == {
        "lines": [{"presentation_id": str(stock), "requested": 3, "available": 1}]
    }
    await assert_no_order_traces(container)
    assert await count(container, "carts") == 1
    assert await count(container, "cart_lines") == 1
    assert await item_row(container, stock) == (1, 0)
    assert len(await outbox_rows(container)) == outbox_before

    await restock(container, stock, 5)
    retry = placed(await real_place_order(container).execute(token, request("home", 768_000)))
    assert retry.number == "LFI-1001"


async def test_another_orders_reservation_makes_the_checkout_short(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Taken", sizes=({"stock_available": 3},))
    await restock(container, stock, 3)
    first = await add_to_cart(container, [(stock, 3)])
    second = await add_to_cart(container, [(stock, 1)])
    use_case = real_place_order(container)
    placed(await use_case.execute(first, request("home", 768_000)))

    error = refusal(await use_case.execute(second, request("home", 268_000)))

    assert error.code == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert error.details == {
        "lines": [{"presentation_id": str(stock), "requested": 1, "available": 0}]
    }
    assert await count(container, "orders") == 1
    assert await count(container, "carts") == 1
    assert await last_number(container) == 1001


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("no_cart", "ORDERS_CHECKOUT_CART_EMPTY"),
        ("wrong_total", "ORDERS_CHECKOUT_TOTAL_CHANGED"),
        ("off_list_postal_code", "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE"),
        ("invalid_phone", "ORDERS_CUSTOMER_DATA_INVALID"),
    ],
)
async def test_the_other_refusals_write_nothing_and_keep_the_cart(
    container: Container, catalog_rows: CatalogRows, scenario: str, code: str
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Keep", sizes=({"stock_available": 5},))
    await restock(container, stock, 5)
    token = await add_to_cart(container, [(stock, 1)])
    use_case = real_place_order(container)
    asked: Any = {
        "no_cart": (None, request("home", 268_000)),
        "wrong_total": (token, request("home", 1)),
        "off_list_postal_code": (token, request("local", 259_000)),
        "invalid_phone": (token, request("home", 268_000, who=contact(phone="123"))),
    }[scenario]

    error = refusal(await use_case.execute(*asked))

    assert error.code == code
    await assert_no_order_traces(container)
    assert await count(container, "carts") == 1
    assert await count(container, "cart_lines") == 1
    assert await item_row(container, stock) == (5, 0)


async def test_a_sold_out_line_refuses_the_checkout_listing_it(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Out", sizes=({"stock_available": 5},))
    token = await add_to_cart(container, [(stock, 1)])
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.presentations SET stock_available = 0 WHERE id = :id"),
            {"id": stock},
        )

    error = refusal(await real_place_order(container).execute(token, request("home", 268_000)))

    assert error.code == "ORDERS_CHECKOUT_CART_HAS_PROBLEMS"
    assert error.details == {"lines": [{"presentation_id": str(stock), "status": "sold_out"}]}
    await assert_no_order_traces(container)
    assert await count(container, "carts") == 1


class ExplodingOrders(SqlOrderRepository):
    """Fails after the number was taken and the stock held, like a crash mid-transaction."""

    async def add(self, order: Order) -> None:
        raise RuntimeError("the database went away")


async def test_a_failure_after_the_hold_rolls_back_the_reservation_the_number_and_the_cart(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Boom", sizes=({"stock_available": 5},))
    await restock(container, stock, 5)
    token = await add_to_cart(container, [(stock, 2)])
    use_case = real_place_order(container, ExplodingOrders(container.database))

    with pytest.raises(RuntimeError, match="went away"):
        await use_case.execute(token, request("home", 518_000))

    await assert_no_order_traces(container)
    assert await item_row(container, stock) == (5, 0)
    assert await table_count(container, "reservation_lines") == 0
    assert await count(container, "carts") == 1
    assert await count(container, "cart_lines") == 1


# --- concurrency --------------------------------------------------------------------------------


async def test_two_checkouts_for_the_last_units_let_exactly_one_win(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Last", sizes=({"stock_available": 3},))
    await restock(container, stock, 3)
    first = await add_to_cart(container, [(stock, 2)])
    second = await add_to_cart(container, [(stock, 2)])
    use_case = real_place_order(container)

    results = await asyncio.wait_for(
        asyncio.gather(
            use_case.execute(first, request("home", 518_000)),
            use_case.execute(second, request("home", 518_000)),
        ),
        TIMEOUT,
    )

    winners = [r for r in results if isinstance(r, Ok)]
    losers = [r for r in results if isinstance(r, Err)]
    assert (len(winners), len(losers)) == (1, 1)
    assert losers[0].error.code == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert winners[0].value.number == "LFI-1001"
    assert await item_row(container, stock) == (3, 2)
    assert await count(container, "orders") == 1
    assert await count(container, "carts") == 1
    assert await last_number(container) == 1001


async def test_concurrent_checkouts_get_distinct_consecutive_numbers_with_no_gaps(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await configure_delivery(container)
    _, [stock] = await catalog_rows.perfume("Many", sizes=({"stock_available": 50},))
    await restock(container, stock, 50)
    tokens = [await add_to_cart(container, [(stock, 1)]) for _ in range(5)]
    use_case = real_place_order(container)

    results = await asyncio.wait_for(
        asyncio.gather(*(use_case.execute(t, request("home", 268_000)) for t in tokens)), TIMEOUT
    )

    numbers = sorted(placed(r).number for r in results)
    assert numbers == [f"LFI-{n}" for n in range(1001, 1006)]
    assert await last_number(container) == 1005
    assert await count(container, "orders") == 5
    assert await count(container, "carts") == 0
    assert await item_row(container, stock) == (50, 5)
