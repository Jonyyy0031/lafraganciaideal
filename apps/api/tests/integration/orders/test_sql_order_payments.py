"""`SqlCustomerOrders.payable_by_access_digest` and the `OrderPayments` facade over real orders
(`fragancia_test`): the lookup is by the digest of the token, the lines come in position order with
their frozen prices, and the figures are the stored ones."""

from datetime import timedelta

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.orders import OrderPayments, PayableLine, PayableOrder
from fragancia_api.modules.orders.domain.order import OrderStatus
from fragancia_api.modules.orders.infrastructure.sql_customer_orders import SqlCustomerOrders
from fragancia_api.modules.orders.infrastructure.sql_order_repository import SqlOrderRepository
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import (
    T0,
    make_order,
    order_line,
    store_order,
)
from tests.unit.orders.support import FakeOrderLinks

pytestmark = pytest.mark.integration

LINKS = FakeOrderLinks()


def queries(container: Container) -> SqlCustomerOrders:
    return SqlCustomerOrders(container.database)


async def test_an_unknown_digest_finds_nothing(container: Container) -> None:
    await store_order(container, make_order(digest="a" * 64))

    assert await queries(container).payable_by_access_digest("b" * 64) is None


async def test_a_home_order_is_found_with_its_frozen_figures(container: Container) -> None:
    order = make_order(digest="a" * 64)
    await store_order(container, order)

    found = await queries(container).payable_by_access_digest("a" * 64)

    assert found == PayableOrder(
        order_id=order.id,
        number="LFI-1001",
        status="pending_payment",
        payment_due_at=T0 + timedelta(minutes=30),
        customer_name="Ana López",
        customer_email="ana@example.com",
        lines=[
            PayableLine(order.lines[0].presentation_id, order.lines[0].label, 2, 250_000),
            PayableLine(order.lines[1].presentation_id, order.lines[1].label, 1, 250_000),
        ],
        delivery_cents=18_000,
        total_cents=768_000,
    )


async def test_a_pickup_order_has_no_delivery_cost(container: Container) -> None:
    await store_order(container, make_order(digest="a" * 64, pickup=True))

    found = await queries(container).payable_by_access_digest("a" * 64)

    assert found is not None
    assert found.delivery_cents == 0
    assert found.total_cents == 750_000


async def test_the_lines_come_in_position_order_with_the_charged_price(
    container: Container,
) -> None:
    lines = (order_line(3, 1), order_line(1, 4), order_line(2, 2))
    await store_order(container, make_order(digest="a" * 64, lines=lines))

    found = await queries(container).payable_by_access_digest("a" * 64)

    assert found is not None
    assert [(line.presentation_id.int, line.quantity) for line in found.lines] == [
        (3, 1),
        (1, 4),
        (2, 2),
    ]
    assert {line.unit_price_cents for line in found.lines} == {250_000}  # not the regular price
    assert (
        sum(line.quantity * line.unit_price_cents for line in found.lines) + found.delivery_cents
        == found.total_cents
    )


async def test_the_status_follows_the_order(container: Container) -> None:
    order = make_order(digest="a" * 64)
    await store_order(container, order)
    repo = SqlOrderRepository(container.database)

    async def cancel() -> None:
        stored = await repo.get_by_number_for_update(1001)
        assert stored is not None
        stored.status = OrderStatus.CANCELLED
        await repo.save(stored)

    await in_transaction(container, cancel)

    found = await queries(container).payable_by_access_digest("a" * 64)
    assert found is not None
    assert found.status == "cancelled"


async def test_each_digest_finds_its_own_order(container: Container) -> None:
    await store_order(container, make_order(number=1001, digest="a" * 64))
    await store_order(container, make_order(number=1002, digest="b" * 64))

    first = await queries(container).payable_by_access_digest("a" * 64)
    second = await queries(container).payable_by_access_digest("b" * 64)

    assert first is not None
    assert second is not None
    assert (first.number, second.number) == ("LFI-1001", "LFI-1002")


async def test_the_facade_looks_the_order_up_by_the_digest_of_the_token(
    container: Container,
) -> None:
    await store_order(container, make_order(digest=LINKS.digest("the-token")))
    facade = OrderPayments(queries=queries(container), links=LINKS)

    found = await facade.payable("the-token")

    assert found is not None
    assert found.number == "LFI-1001"
    assert await facade.payable(LINKS.digest("the-token")) is None
    assert await facade.payable("other") is None
