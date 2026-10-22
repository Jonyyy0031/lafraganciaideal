"""`StartCheckout` (start_checkout.py) with in-memory adapters and the recording fake gateway."""

from datetime import timedelta
from typing import Any

import pytest

from fragancia_api.modules.orders.domain.order import OrderStatus
from fragancia_api.modules.payments.application.commands.start_checkout import checkout_items
from fragancia_api.modules.payments.application.ports import CheckoutRequest
from fragancia_api.modules.payments.domain.checkout import REUSE_MARGIN
from fragancia_api.modules.payments.domain.errors import (
    PaymentGatewayUnavailable,
    PaymentOrderNotFound,
    PaymentOrderNotPayable,
)
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.payments.support import (
    BACK_URL,
    NOW,
    ORDER_ID,
    TOKEN,
    World,
    make_checkout,
    make_line,
    make_payable,
)

SECOND = timedelta(seconds=1)
NOT_PENDING = [s.value for s in OrderStatus if s is not OrderStatus.PENDING_PAYMENT]


def refused(result: Result[Any, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


def answer(result: Result[str, DomainError]) -> str:
    assert isinstance(result, Ok), result
    return result.value


# --- refusals ----------------------------------------------------------------------------------


async def test_an_unknown_token_is_order_not_found() -> None:
    world = World()
    world.put(make_payable())

    error = refused(await world.start_checkout().execute("nope"))

    assert isinstance(error, PaymentOrderNotFound)
    assert error.code == "PAYMENTS_ORDER_NOT_FOUND"
    assert world.gateway.requests == []
    assert world.checkouts.by_order == {}


@pytest.mark.parametrize("status", NOT_PENDING)
async def test_an_order_that_is_not_pending_payment_is_not_payable(status: str) -> None:
    world = World()
    world.put(make_payable(status=status))

    error = refused(await world.start_checkout().execute(TOKEN))

    assert isinstance(error, PaymentOrderNotPayable)
    assert error.code == "PAYMENTS_ORDER_NOT_PAYABLE"
    assert error.details == {"status": status}
    assert world.gateway.requests == []
    assert world.checkouts.by_order == {}


@pytest.mark.parametrize("due", [timedelta(0), -SECOND, -timedelta(hours=2)])
async def test_an_order_at_or_past_its_deadline_is_not_payable(due: timedelta) -> None:
    world = World()
    world.put(make_payable(due=due))

    error = refused(await world.start_checkout().execute(TOKEN))

    assert isinstance(error, PaymentOrderNotPayable)
    assert error.details == {"status": "pending_payment"}
    assert world.gateway.requests == []


async def test_an_order_one_second_before_its_deadline_is_still_payable() -> None:
    world = World()
    world.put(make_payable(due=SECOND))

    assert isinstance(await world.start_checkout().execute(TOKEN), Ok)


async def test_a_valid_stored_link_is_not_reused_for_an_order_that_is_no_longer_payable() -> None:
    world = World()
    world.put(make_payable(status="cancelled"))
    world.checkouts.by_order[ORDER_ID] = make_checkout(NOW + timedelta(minutes=20))

    error = refused(await world.start_checkout().execute(TOKEN))

    assert isinstance(error, PaymentOrderNotPayable)


# --- a new link --------------------------------------------------------------------------------


async def test_a_payable_order_gets_a_link_that_is_stored_with_the_payment_deadline() -> None:
    world = World()
    order = make_payable(due=timedelta(minutes=30))
    world.put(order)

    url = answer(await world.start_checkout().execute(TOKEN))

    assert url == "https://pay.test/checkout/pref-1"
    stored = world.checkouts.by_order[ORDER_ID]
    assert (stored.preference_id, stored.redirect_url) == ("pref-1", url)
    assert stored.expires_at == order.payment_due_at
    assert stored.created_at == NOW


async def test_the_gateway_receives_the_order_facts_and_expires_at_the_payment_deadline() -> None:
    world = World()
    order = make_payable()
    world.put(order)

    await world.start_checkout().execute(TOKEN)

    [request] = world.gateway.requests
    assert isinstance(request, CheckoutRequest)
    assert request.order_id == ORDER_ID
    assert request.number == "LFI-1001"
    assert (request.payer_name, request.payer_email) == ("Ana López", "ana@example.com")
    assert request.expires_at == order.payment_due_at
    assert request.back_url == BACK_URL


async def test_the_gateway_is_called_outside_the_transaction() -> None:
    world = World()
    world.put(make_payable())
    depth = {"now": 0}
    inside: list[bool] = []

    class Runner:
        async def run(self, work: Any) -> Any:
            depth["now"] += 1
            try:
                return await work()
            finally:
                depth["now"] -= 1

    class WatchingGateway(FakeGateway):
        async def create_checkout(self, request: CheckoutRequest) -> Any:
            inside.append(depth["now"] > 0)
            return await super().create_checkout(request)

    use_case = world.start_checkout(WatchingGateway())
    use_case._transactions = Runner()  # noqa: SLF001

    assert isinstance(await use_case.execute(TOKEN), Ok)
    assert inside == [False]


# --- items -------------------------------------------------------------------------------------


async def test_the_items_are_the_lines_plus_the_shipping_and_sum_to_the_total() -> None:
    world = World()
    order = make_payable(delivery_cents=18_000)
    world.put(order)

    await world.start_checkout().execute(TOKEN)

    [request] = world.gateway.requests
    assert [(i.id, i.title, i.quantity, i.unit_price_cents) for i in request.items] == [
        (str(make_line(1).presentation_id), "Brand Perfume 1 EDT 100 ml", 2, 250_000),
        (str(make_line(2).presentation_id), "Brand Perfume 2 EDT 100 ml", 1, 99_900),
        ("shipping", "Envío", 1, 18_000),
    ]
    assert sum(i.quantity * i.unit_price_cents for i in request.items) == order.total_cents


async def test_a_free_delivery_adds_no_shipping_item() -> None:
    world = World()
    order = make_payable(delivery_cents=0)
    world.put(order)

    await world.start_checkout().execute(TOKEN)

    [request] = world.gateway.requests
    assert all(i.id != "shipping" for i in request.items)
    assert len(request.items) == 2
    assert sum(i.quantity * i.unit_price_cents for i in request.items) == order.total_cents


def test_items_that_do_not_add_up_to_the_total_are_refused_by_an_assertion() -> None:
    with pytest.raises(AssertionError, match="add up"):
        checkout_items(make_payable(total_cents=1))


async def test_a_total_mismatch_never_reaches_the_gateway() -> None:
    world = World()
    world.put(make_payable(total_cents=1))

    with pytest.raises(AssertionError):
        await world.start_checkout().execute(TOKEN)

    assert world.gateway.requests == []
    assert world.checkouts.by_order == {}


# --- reuse and the margin ----------------------------------------------------------------------


async def test_a_second_request_reuses_the_stored_link_without_calling_the_gateway() -> None:
    world = World()
    world.put(make_payable())
    use_case = world.start_checkout()

    first = answer(await use_case.execute(TOKEN))
    second = answer(await use_case.execute(TOKEN))

    assert second == first
    assert len(world.gateway.requests) == 1


async def test_a_checkout_just_outside_the_margin_is_reused() -> None:
    world = World()
    world.put(make_payable())
    world.checkouts.by_order[ORDER_ID] = make_checkout(NOW + REUSE_MARGIN + SECOND)

    url = answer(await world.start_checkout().execute(TOKEN))

    assert url == "https://pay.test/pref-stored"
    assert world.gateway.requests == []


@pytest.mark.parametrize("offset", [REUSE_MARGIN, REUSE_MARGIN - SECOND, timedelta(0), -SECOND])
async def test_a_checkout_at_or_inside_the_margin_is_replaced_by_a_new_one(
    offset: timedelta,
) -> None:
    world = World()
    order = make_payable()
    world.put(order)
    world.checkouts.by_order[ORDER_ID] = make_checkout(NOW + offset)

    url = answer(await world.start_checkout().execute(TOKEN))

    assert url == "https://pay.test/checkout/pref-1"
    assert len(world.gateway.requests) == 1
    stored = world.checkouts.by_order[ORDER_ID]
    assert (stored.preference_id, stored.expires_at) == ("pref-1", order.payment_due_at)
    assert len(world.checkouts.by_order) == 1


async def test_a_link_is_reused_until_two_minutes_before_it_expires() -> None:
    world = World()
    world.put(make_payable(due=timedelta(minutes=30)))
    use_case = world.start_checkout()
    first = answer(await use_case.execute(TOKEN))

    world.clock.current = NOW + timedelta(minutes=28) - SECOND
    assert answer(await use_case.execute(TOKEN)) == first
    world.clock.current = NOW + timedelta(minutes=28)
    assert answer(await use_case.execute(TOKEN)) != first
    assert len(world.gateway.requests) == 2


# --- gateway failure ---------------------------------------------------------------------------


async def test_a_gateway_failure_is_answered_and_stores_nothing() -> None:
    world = World(gateway=FakeGateway(fail=True))
    world.put(make_payable())

    error = refused(await world.start_checkout().execute(TOKEN))

    assert isinstance(error, PaymentGatewayUnavailable)
    assert error.code == "PAYMENTS_GATEWAY_UNAVAILABLE"
    assert len(world.gateway.requests) == 1
    assert world.checkouts.by_order == {}


async def test_a_gateway_failure_keeps_the_expiring_checkout_that_was_stored() -> None:
    world = World(gateway=FakeGateway(fail=True))
    world.put(make_payable())
    old = make_checkout(NOW + SECOND)
    world.checkouts.by_order[ORDER_ID] = old

    error = refused(await world.start_checkout().execute(TOKEN))

    assert isinstance(error, PaymentGatewayUnavailable)
    assert world.checkouts.by_order == {ORDER_ID: old}
