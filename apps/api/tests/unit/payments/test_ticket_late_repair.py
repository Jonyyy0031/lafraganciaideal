"""Plan 003 step 9 (re-review repair): R1, the pending query caps each ticket's `expires_at` at
the order's `payment_due_at`, less `TICKET_GRACE` since step 11 (application and http); R2,
the handler publishes `payments.ticket.cancel_requested` only when the hold answers
`not_pending`, and `CancelTicket` cancels the ticket in Mercado Pago and raises on a gateway
error."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.payments.application.handlers.cancel_ticket import CancelTicket
from fragancia_api.modules.payments.application.queries.pending_tickets import GetPendingTickets
from fragancia_api.modules.payments.domain.events import TicketCancelRequested
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE, Payment
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.modules.payments.infrastructure.in_memory import (
    FakeGateway,
    InMemoryPayableOrders,
    InMemoryPayments,
)
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Ok
from tests.support import TestActorResolver
from tests.unit.payments.support import NOW, TOKEN, make_payable
from tests.unit.payments.test_pending_tickets import payment
from tests.unit.payments.test_process_payment import PAYMENT_ID, World, message
from tests.unit.payments.test_process_payment_tickets import (
    HoldingConfirmations,
    pending_ticket,
)

PENDING = "/api/v1/payments/pending"


def query(due: timedelta, *payments: Payment) -> GetPendingTickets:
    """`due` is the last moment to pay (step 11, S1): the order's deadline is `due` plus
    `TICKET_GRACE`, and the query caps each expiry at the deadline minus the grace."""
    orders = InMemoryPayableOrders()
    orders.by_token[TOKEN] = make_payable(due=due + TICKET_GRACE)
    return GetPendingTickets(
        orders=orders, queries=InMemoryPayments(*payments), clock=FixedClock(NOW)
    )


async def expiries(use_case: GetPendingTickets) -> list[object]:
    result = await use_case.execute(TOKEN)
    assert isinstance(result, Ok), result
    return [item.expires_at for item in result.value.items]


# --- R1 ----------------------------------------------------------------------------------------


async def test_a_ticket_expiring_after_the_order_deadline_shows_the_deadline() -> None:
    use_case = query(timedelta(hours=10), payment("1", expires_at=NOW + timedelta(hours=48)))

    assert await expiries(use_case) == [NOW + timedelta(hours=10)]


async def test_a_ticket_expiring_before_the_order_deadline_keeps_its_own_expiry() -> None:
    use_case = query(timedelta(hours=48), payment("1", expires_at=NOW + timedelta(hours=5)))

    assert await expiries(use_case) == [NOW + timedelta(hours=5)]


async def test_a_ticket_expiring_exactly_at_the_deadline_shows_that_instant() -> None:
    at = NOW + timedelta(hours=12)

    assert await expiries(query(timedelta(hours=12), payment("1", expires_at=at))) == [at]


async def test_each_ticket_is_capped_on_its_own_and_the_other_fields_are_untouched() -> None:
    use_case = query(
        timedelta(hours=20),
        payment("a", expires_at=NOW + timedelta(hours=48), created_at=NOW, ticket_url="u-a"),
        payment(
            "b",
            expires_at=NOW + timedelta(hours=6),
            created_at=NOW - timedelta(hours=1),
            ticket_url="u-b",
            payment_type="bank_transfer",
            amount_cents=5,
        ),
    )

    result = await use_case.execute(TOKEN)

    assert isinstance(result, Ok)
    first, second = result.value.items
    assert (first.ticket_url, first.expires_at) == ("u-a", NOW + timedelta(hours=20))
    assert (second.ticket_url, second.expires_at) == ("u-b", NOW + timedelta(hours=6))
    assert (second.method, second.amount_cents) == ("transferencia SPEI", 5)


async def test_the_cap_does_not_change_which_tickets_are_listed() -> None:
    use_case = query(
        timedelta(hours=10),
        payment("live", expires_at=NOW + timedelta(hours=48)),
        payment("past", expires_at=NOW - timedelta(hours=1)),
    )

    assert len(await expiries(use_case)) == 1


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        GetPendingTickets,
        query(timedelta(hours=10), payment("1", expires_at=NOW + timedelta(hours=48))),
    )
    app = build_app(services, routers)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_the_http_answer_carries_the_capped_expiry(client: AsyncClient) -> None:
    response = await client.post(PENDING, json={"access_token": TOKEN})

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert item["expires_at"] == "2026-10-22T22:00:00Z"


async def test_the_contract_describes_the_expiry_as_the_earlier_of_ticket_and_deadline() -> None:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    schema = build_app(services, routers).openapi()

    description = schema["components"]["schemas"]["PendingTicket"]["properties"]["expires_at"][
        "description"
    ]

    assert "whichever is earlier" in description


# --- R2: the event -----------------------------------------------------------------------------


def cancel_requests(world: World) -> list[dict[str, object]]:
    assert all(isinstance(e, TicketCancelRequested) for e in world.events.published)
    return [e.payload() for e in world.events.published]


def ticket_world(outcome: str, **overrides: object) -> World:
    return World(fetched=pending_ticket(**overrides), confirmations=HoldingConfirmations(outcome))


async def test_a_ticket_for_an_order_that_is_not_pending_requests_its_cancel() -> None:
    world = ticket_world("not_pending")

    await world.handler(message())

    assert cancel_requests(world) == [{"payment_id": PAYMENT_ID}]
    assert TicketCancelRequested.name == "payments.ticket.cancel_requested"
    assert world.row().held_at == NOW


async def test_a_redelivery_does_not_request_the_cancel_again() -> None:
    world = ticket_world("not_pending")

    await world.handler(message())
    await world.handler(message())

    assert len(cancel_requests(world)) == 1


@pytest.mark.parametrize("outcome", ["held", "not_found"])
async def test_a_held_or_unknown_order_requests_no_cancel(outcome: str) -> None:
    world = ticket_world(outcome)

    await world.handler(message())

    assert world.events.published == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"payment_type": "credit_card"},
        {"payment_type": "atm"},
        {"status": "cancelled"},
        {"expires_at": NOW - timedelta(hours=1)},
    ],
)
async def test_a_payment_that_does_not_need_a_hold_requests_no_cancel(
    overrides: dict[str, object],
) -> None:
    world = ticket_world("not_pending", **overrides)

    await world.handler(message())

    assert world.events.published == []


async def test_a_refusal_from_orders_requests_no_cancel() -> None:
    from fragancia_api.shared.kernel import ConflictError

    refusal = ConflictError(code="ORDERS_TRANSITION_INVALID", message="refused")
    world = World(fetched=pending_ticket(), confirmations=HoldingConfirmations("held", refusal))

    with pytest.raises(RuntimeError):
        await world.handler(message())

    assert world.events.published == []


# --- R2: CancelTicket --------------------------------------------------------------------------


def cancel_message(payment_id: str = "555") -> EventMessage:
    e = TicketCancelRequested(payment_id=payment_id)
    return EventMessage(id=e.event_id, name=e.name, payload=e.payload(), occurred_at=e.occurred_at)


async def test_it_cancels_the_ticket_of_the_event() -> None:
    gateway = FakeGateway()

    await CancelTicket(gateway=gateway)(cancel_message("555"))

    assert gateway.cancelled == ["555"]


async def test_a_refused_cancel_is_not_an_error() -> None:
    gateway = FakeGateway()
    gateway.refused_cancels.add("555")

    await CancelTicket(gateway=gateway)(cancel_message("555"))

    assert gateway.cancelled == ["555"]


async def test_a_gateway_error_raises_so_the_event_retries() -> None:
    gateway = FakeGateway(fail=True)

    with pytest.raises(RuntimeError, match=r"^payments\.gateway_unavailable$"):
        await CancelTicket(gateway=gateway)(cancel_message())
