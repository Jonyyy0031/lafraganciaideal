"""`OrderPayments` (order_payments.py) and `InMemoryCustomerOrders.payable_by_access_digest`: the
facade payments reads an order through."""

from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.orders import OrderPayments, PayableLine, PayableOrder
from fragancia_api.modules.orders.domain.order import OrderStatus
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryCustomerOrders
from tests.unit.orders.support import NOW, FakeOrderLinks
from tests.unit.orders.test_order_lifecycle import World

LINKS = FakeOrderLinks()


async def stored(world: World, status: OrderStatus = OrderStatus.PENDING_PAYMENT, n: int = 1):  # type: ignore[no-untyped-def]
    order = await world.store(status, n=n)
    world.current(n).access_token_digest = LINKS.digest(f"tok-{n}")
    return order


def facade(world: World) -> OrderPayments:
    return OrderPayments(queries=InMemoryCustomerOrders(world.orders), links=LINKS)


async def test_an_unknown_token_gives_none() -> None:
    world = World()
    await stored(world)

    assert await facade(world).payable("nope") is None


async def test_the_stored_digest_presented_as_a_token_gives_none() -> None:
    world = World()
    await stored(world)
    digest = world.current().access_token_digest

    assert digest is not None
    assert await facade(world).payable(digest) is None


async def test_the_token_finds_the_order_facts_payments_needs() -> None:
    world = World()
    order = await stored(world)

    found = await facade(world).payable("tok-1")

    assert isinstance(found, PayableOrder)
    assert found.order_id == UUID(int=901)
    assert found.number == "LFI-1001"
    assert found.status == "pending_payment"
    assert found.payment_due_at == NOW + timedelta(minutes=30)
    assert (found.customer_name, found.customer_email) == (order.name.value, order.email.value)
    assert found.delivery_cents == order.delivery.cost_cents
    assert found.total_cents == order.total_cents
    assert found.lines == [
        PayableLine(
            presentation_id=line.presentation_id,
            label=line.label,
            quantity=line.quantity,
            unit_price_cents=line.unit_price_cents,
        )
        for line in order.lines
    ]


async def test_the_status_is_the_current_one() -> None:
    world = World()
    await stored(world, OrderStatus.CANCELLED)

    found = await facade(world).payable("tok-1")

    assert found is not None
    assert found.status == "cancelled"


async def test_each_token_finds_its_own_order() -> None:
    world = World()
    await stored(world, n=1)
    await stored(world, n=2)

    first = await facade(world).payable("tok-1")
    second = await facade(world).payable("tok-2")

    assert first is not None
    assert second is not None
    assert (first.number, second.number) == ("LFI-1001", "LFI-1002")


async def test_the_frozen_total_is_the_lines_plus_the_delivery() -> None:
    world = World()
    await stored(world)

    found = await facade(world).payable("tok-1")

    assert found is not None
    assert (
        sum(line.quantity * line.unit_price_cents for line in found.lines) + found.delivery_cents
        == found.total_cents
    )
