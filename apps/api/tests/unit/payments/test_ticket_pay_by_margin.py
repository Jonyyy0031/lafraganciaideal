"""Plan 003 step 11 (S1): the deadline told to the customer leaves the credit margin.
`pay_by = until - TICKET_GRACE` (a single ticket: its own expiry; a second ticket: the earlier
of its own and the first ticket's expiry), checked through the email text, and `/payments/pending`
caps each ticket at `payment_due_at - TICKET_GRACE` (application and http)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.handlers.order_emails import (
    payment_instructions_email,
)
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
from tests.unit.orders.test_order_emails import LINK, make_detail
from tests.unit.payments.support import NOW, TOKEN, make_payable
from tests.unit.payments.test_pending_tickets import payment
from tests.unit.payments.test_process_payment import World, message
from tests.unit.payments.test_process_payment_tickets import pending_ticket
from tests.unit.payments.test_ticket_grace import PayByRecording

# Cancún is UTC-5 without daylight saving time.
FIRST = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)  # 13:00 on the 29th in Cancún


def told(pay_by: datetime) -> str:
    """The deadline as the customer's email prints it."""
    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", pay_by, None)
    return body.split("antes del ")[1].split(".\n")[0]


async def second_ticket_pay_by(second_expiry: datetime) -> tuple[datetime, datetime]:
    """(until, pay_by) given to orders for a second ticket after a first one expiring at FIRST."""
    confirmations = PayByRecording()
    world = World(fetched=pending_ticket(expires_at=FIRST), confirmations=confirmations)
    await world.handler(message())
    world.gateway.payments["999"] = pending_ticket(payment_id="999", expires_at=second_expiry)
    await world.handler(message("999"))
    return confirmations.holds[1][1], confirmations.pay_bys[1]


async def test_a_single_ticket_tells_its_own_expiry_in_the_email() -> None:
    confirmations = PayByRecording()
    world = World(fetched=pending_ticket(expires_at=FIRST), confirmations=confirmations)

    await world.handler(message())

    [until] = [hold[1] for hold in confirmations.holds]
    assert confirmations.pay_bys == [until - TICKET_GRACE] == [FIRST]
    assert told(confirmations.pay_bys[0]) == "29/10/2026 a las 13:00"


@pytest.mark.parametrize("hours", [10, 47, 60, 200])
async def test_a_later_ticket_tells_the_first_expiry_not_its_own_nor_the_cancel_time(
    hours: int,
) -> None:
    until, pay_by = await second_ticket_pay_by(FIRST + timedelta(hours=hours))

    assert until == FIRST + TICKET_GRACE
    assert pay_by == until - TICKET_GRACE == FIRST
    assert told(pay_by) == "29/10/2026 a las 13:00"


@pytest.mark.parametrize("hours", [1, 12, 40])
async def test_a_second_ticket_expiring_earlier_tells_its_own_expiry(hours: int) -> None:
    earlier = FIRST - timedelta(hours=hours)

    until, pay_by = await second_ticket_pay_by(earlier)

    assert until == earlier + TICKET_GRACE
    assert pay_by == earlier == until - TICKET_GRACE
    cancun = earlier.astimezone(timezone(timedelta(hours=-5)))
    assert told(pay_by) == cancun.strftime("%d/%m/%Y a las %H:%M")


# --- /payments/pending -------------------------------------------------------------------------


def query(due: timedelta, *payments) -> GetPendingTickets:  # type: ignore[no-untyped-def]
    orders = InMemoryPayableOrders()
    orders.by_token[TOKEN] = make_payable(due=due)
    return GetPendingTickets(
        orders=orders, queries=InMemoryPayments(*payments), clock=FixedClock(NOW)
    )


async def shown(use_case: GetPendingTickets) -> list[datetime]:
    result = await use_case.execute(TOKEN)
    assert isinstance(result, Ok), result
    return [item.expires_at for item in result.value.items]


async def test_a_ticket_after_the_deadline_less_the_margin_shows_that_instant() -> None:
    use_case = query(timedelta(hours=100), payment("1", expires_at=NOW + timedelta(hours=90)))

    assert await shown(use_case) == [NOW + timedelta(hours=100) - TICKET_GRACE]


async def test_a_ticket_inside_the_deadline_less_the_margin_keeps_its_own_expiry() -> None:
    use_case = query(timedelta(hours=100), payment("1", expires_at=NOW + timedelta(hours=20)))

    assert await shown(use_case) == [NOW + timedelta(hours=20)]


async def test_a_ticket_expiring_exactly_at_the_deadline_less_the_margin_shows_it() -> None:
    at = NOW + timedelta(hours=100) - TICKET_GRACE

    assert await shown(query(timedelta(hours=100), payment("1", expires_at=at))) == [at]


async def test_the_deadline_itself_is_never_shown() -> None:
    use_case = query(timedelta(hours=100), payment("1", expires_at=NOW + timedelta(hours=500)))

    [expiry] = await shown(use_case)

    assert expiry < NOW + timedelta(hours=100)


@pytest.mark.parametrize(("ticket_hours", "shown_hours"), [(30, 30), (90, 52)])
async def test_the_http_answer_caps_at_the_deadline_less_the_margin(
    ticket_hours: int, shown_hours: int
) -> None:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        GetPendingTickets,
        query(timedelta(hours=100), payment("1", expires_at=NOW + timedelta(hours=ticket_hours))),
    )
    async with AsyncClient(
        transport=ASGITransport(app=build_app(services, routers)), base_url="http://t"
    ) as client:
        response = await client.post("/api/v1/payments/pending", json={"access_token": TOKEN})

    [item] = response.json()["items"]
    expected = (NOW + timedelta(hours=shown_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert item["expires_at"] == expected
