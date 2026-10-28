"""Plan 003 step 10 (verification repair), payments: V1 (the close body has no
`date_of_expiration`) and decision 22 (the order waits for the ticket's expiry plus
`TICKET_GRACE`; the decision 19 cap is the first expiry plus the grace; the stored expiry
keeps Mercado Pago's). Adjusted at step 11 (S1): `pay_by` is `until - TICKET_GRACE`, and
`/payments/pending` caps at the order's deadline minus the grace."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.payments.application.queries.pending_tickets import GetPendingTickets
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.modules.payments.infrastructure.in_memory import (
    InMemoryPayableOrders,
    InMemoryPayments,
)
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Ok
from tests.support import TestActorResolver
from tests.unit.payments.support import NOW, TOKEN, make_payable
from tests.unit.payments.test_mercadopago_gateway import answering, body_of, gateway_for
from tests.unit.payments.test_pending_tickets import payment
from tests.unit.payments.test_process_payment import World, message
from tests.unit.payments.test_process_payment_tickets import (
    EXPIRES,
    HoldingConfirmations,
    pending_ticket,
)

AT = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


class PayByRecording(HoldingConfirmations):
    """Also records the `pay_by` of every hold."""

    def __init__(self) -> None:
        super().__init__()
        self.pay_bys: list[datetime] = []

    async def hold(  # type: ignore[no-untyped-def]
        self, order_id, until, pay_by, method, instructions_url
    ):
        self.pay_bys.append(pay_by)
        return await super().hold(order_id, until, pay_by, method, instructions_url)


def second(world: World, expires: datetime, payment_id: str = "999") -> None:
    world.gateway.payments[payment_id] = pending_ticket(payment_id=payment_id, expires_at=expires)


# --- V1 ----------------------------------------------------------------------------------------


async def test_the_close_body_has_no_date_of_expiration() -> None:
    recorder = answering(200, {"id": "pref-1"})

    await gateway_for(recorder).close_checkout("pref-1", AT)

    body = body_of(recorder.sent)
    assert "date_of_expiration" not in body
    assert body == {"expires": True, "expiration_date_to": "2026-10-27T15:00:00.000+00:00"}


# --- decision 22 -------------------------------------------------------------------------------


def test_the_grace_is_forty_eight_hours() -> None:
    assert TICKET_GRACE == timedelta(hours=48)


async def test_a_single_ticket_holds_until_its_expiry_plus_the_grace_and_tells_its_own_expiry() -> (
    None
):
    confirmations = PayByRecording()
    world = World(fetched=pending_ticket(), confirmations=confirmations)

    await world.handler(message())

    [(_, until, _, _)] = confirmations.holds
    assert until == EXPIRES + TICKET_GRACE
    assert confirmations.pay_bys == [EXPIRES]
    assert world.row().expires_at == EXPIRES  # the record keeps Mercado Pago's expiry


async def test_a_later_ticket_is_capped_at_the_first_expiry_plus_the_grace() -> None:
    confirmations = PayByRecording()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    far = EXPIRES + timedelta(hours=60)
    second(world, far)

    await world.handler(message("999"))

    assert confirmations.holds[1][1] == EXPIRES + TICKET_GRACE
    # Step 11 (S1): the email shows the first ticket's expiry, so the margin follows it.
    assert confirmations.pay_bys[1] == EXPIRES
    assert world.payments.by_id["999"].expires_at == far


async def test_a_later_ticket_expiring_inside_the_margin_tells_the_first_expiry() -> None:
    confirmations = PayByRecording()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    inside = EXPIRES + timedelta(hours=10)
    second(world, inside)

    await world.handler(message("999"))

    assert confirmations.holds[1][1] == EXPIRES + TICKET_GRACE  # capped: inside + grace is later
    assert confirmations.pay_bys[1] == EXPIRES  # step 11 (S1): never inside the margin


async def test_a_second_ticket_expiring_earlier_holds_to_its_own_expiry_plus_the_grace() -> None:
    confirmations = PayByRecording()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    earlier = EXPIRES - timedelta(hours=10)
    second(world, earlier)

    await world.handler(message("999"))

    assert confirmations.holds[1][1] == earlier + TICKET_GRACE
    assert confirmations.pay_bys[1] == earlier


# --- /payments/pending keeps Mercado Pago's expiry ---------------------------------------------


def query(due: timedelta, *payments) -> GetPendingTickets:  # type: ignore[no-untyped-def]
    orders = InMemoryPayableOrders()
    orders.by_token[TOKEN] = make_payable(due=due)
    return GetPendingTickets(
        orders=orders, queries=InMemoryPayments(*payments), clock=FixedClock(NOW)
    )


async def test_a_ticket_inside_the_order_deadline_with_the_margin_shows_its_own_expiry() -> None:
    expires = NOW + timedelta(hours=48)
    use_case = query(timedelta(hours=96), payment("1", expires_at=expires))

    result = await use_case.execute(TOKEN)

    assert isinstance(result, Ok)
    assert [i.expires_at for i in result.value.items] == [expires]


async def test_a_ticket_after_the_order_deadline_less_the_margin_is_capped_there() -> None:
    use_case = query(timedelta(hours=96), payment("1", expires_at=NOW + timedelta(hours=120)))

    result = await use_case.execute(TOKEN)

    assert isinstance(result, Ok)
    # Step 11 (S1): the cap is the order's deadline minus `TICKET_GRACE`.
    assert [i.expires_at for i in result.value.items] == [NOW + timedelta(hours=48)]


@pytest.mark.parametrize(("ticket_hours", "shown_hours"), [(48, 48), (100, 48)])
async def test_the_http_answer_shows_the_earlier_of_the_ticket_and_the_deadline(
    ticket_hours: int, shown_hours: int
) -> None:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        GetPendingTickets,
        query(timedelta(hours=96), payment("1", expires_at=NOW + timedelta(hours=ticket_hours))),
    )
    async with AsyncClient(
        transport=ASGITransport(app=build_app(services, routers)), base_url="http://t"
    ) as client:
        response = await client.post("/api/v1/payments/pending", json={"access_token": TOKEN})

    [item] = response.json()["items"]
    shown = (NOW + timedelta(hours=shown_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert item["expires_at"] == shown
