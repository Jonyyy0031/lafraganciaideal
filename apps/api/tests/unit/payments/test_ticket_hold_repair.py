"""Plan 003 step 8 (review repair), domain and application: `Payment.needs_hold(now)` and
`expired_on_arrival` (L1), `InMemoryPayments.first_hold_expiry` and `pending_cash` (the fake
mirrors the SQL adapter), the cap at the first hold (M1, decision 19), the expired-on-arrival
ticket (no hold, no email), and `CloseCheckout` cancelling the order's pending tickets
(decision 20)."""

from datetime import timedelta
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.handlers.close_checkout import CloseCheckout
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE
from fragancia_api.modules.payments.infrastructure.in_memory import (
    FakeGateway,
    InMemoryCheckouts,
    InMemoryPayments,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.unit.payments.support import NOW, ORDER_ID, make_checkout
from tests.unit.payments.test_close_checkout import close_requested
from tests.unit.payments.test_payment_tickets_domain import EXPIRES, ticket
from tests.unit.payments.test_process_payment import PAYMENT_ID, World, message
from tests.unit.payments.test_process_payment_tickets import (
    HoldingConfirmations,
    pending_ticket,
)

OTHER = UUID(int=901)


# --- L1: the domain ----------------------------------------------------------------------------


def test_a_ticket_expiring_exactly_now_or_before_needs_no_hold() -> None:
    assert ticket(expires_at=NOW).needs_hold(NOW) is False
    assert ticket(expires_at=NOW - timedelta(seconds=1)).needs_hold(NOW) is False
    assert ticket(expires_at=NOW + timedelta(seconds=1)).needs_hold(NOW) is True


@pytest.mark.parametrize("kind", ["ticket", "bank_transfer"])
def test_a_pending_cash_ticket_not_held_and_expired_is_expired_on_arrival(kind: str) -> None:
    assert ticket(payment_type=kind, expires_at=NOW).expired_on_arrival(NOW) is True
    assert ticket(payment_type=kind, expires_at=NOW - timedelta(hours=1)).expired_on_arrival(NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # still live
        {"expires_at": NOW - timedelta(hours=1), "held_at": NOW},
        {"expires_at": NOW - timedelta(hours=1), "status": "approved"},
        {"expires_at": NOW - timedelta(hours=1), "status": "cancelled"},
        {"expires_at": NOW - timedelta(hours=1), "payment_type": "credit_card"},
        {"expires_at": NOW - timedelta(hours=1), "payment_type": None},
        {"expires_at": None},
    ],
)
def test_anything_else_is_not_expired_on_arrival(overrides: dict[str, object]) -> None:
    assert ticket(**overrides).expired_on_arrival(NOW) is False


def test_a_ticket_is_never_both_needing_a_hold_and_expired_on_arrival() -> None:
    for delta in (-timedelta(hours=1), timedelta(0), timedelta(hours=1)):
        payment = ticket(expires_at=NOW + delta)
        assert payment.needs_hold(NOW) != payment.expired_on_arrival(NOW)


# --- the fake repository (same rules as the SQL adapter) ---------------------------------------


async def test_first_hold_expiry_is_none_without_a_held_payment() -> None:
    payments = InMemoryPayments(ticket(payment_id="a"))

    assert await payments.first_hold_expiry(ORDER_ID) is None


async def test_first_hold_expiry_is_the_expiry_of_the_earliest_held_payment() -> None:
    late = ticket(
        payment_id="late", held_at=NOW + timedelta(hours=1), expires_at=EXPIRES + timedelta(hours=1)
    )
    early = ticket(payment_id="early", held_at=NOW, expires_at=EXPIRES)
    payments = InMemoryPayments(late, early)

    assert await payments.first_hold_expiry(ORDER_ID) == EXPIRES


async def test_first_hold_expiry_breaks_a_tie_by_payment_id() -> None:
    payments = InMemoryPayments(
        ticket(payment_id="b", held_at=NOW, expires_at=EXPIRES + timedelta(hours=2)),
        ticket(payment_id="a", held_at=NOW, expires_at=EXPIRES),
    )

    assert await payments.first_hold_expiry(ORDER_ID) == EXPIRES


async def test_first_hold_expiry_ignores_an_expired_on_arrival_ticket() -> None:
    gone = ticket(payment_id="gone", held_at=NOW, expires_at=NOW - timedelta(hours=1))
    equal = ticket(payment_id="equal", held_at=NOW, expires_at=NOW)
    real = ticket(payment_id="real", held_at=NOW + timedelta(hours=1), expires_at=EXPIRES)
    payments = InMemoryPayments(gone, equal, real)

    assert await payments.first_hold_expiry(ORDER_ID) == EXPIRES
    assert await InMemoryPayments(gone, equal).first_hold_expiry(ORDER_ID) is None


async def test_first_hold_expiry_looks_only_at_the_asked_order() -> None:
    payments = InMemoryPayments(ticket(payment_id="a", held_at=NOW, order_id=OTHER))

    assert await payments.first_hold_expiry(ORDER_ID) is None
    assert await payments.first_hold_expiry(OTHER) == EXPIRES


async def test_pending_cash_lists_pending_tickets_whatever_their_expiry_oldest_first() -> None:
    payments = InMemoryPayments(
        ticket(payment_id="new", created_at=NOW),
        ticket(payment_id="old", created_at=NOW - timedelta(hours=1), payment_type="bank_transfer"),
        ticket(
            payment_id="past",
            created_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(days=1),
        ),
        ticket(payment_id="held", created_at=NOW + timedelta(hours=1), held_at=NOW),
    )

    found = await payments.pending_cash(ORDER_ID)

    assert [p.payment_id for p in found] == ["past", "old", "new", "held"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "cancelled"},
        {"payment_type": "credit_card"},
        {"payment_type": "atm"},
        {"payment_type": None},
        {"order_id": OTHER},
        {"order_id": None},
    ],
)
async def test_pending_cash_leaves_out_everything_else(overrides: dict[str, object]) -> None:
    assert await InMemoryPayments(ticket(**overrides)).pending_cash(ORDER_ID) == []


async def test_pending_cash_hands_out_copies() -> None:
    payments = InMemoryPayments(ticket(payment_id="a"))

    [found] = await payments.pending_cash(ORDER_ID)
    found.status = "approved"

    assert payments.by_id["a"].status == "pending"


# --- M1: the cap at the first hold -------------------------------------------------------------


def second_ticket(world: World, expires: object, payment_id: str = "999") -> None:
    world.gateway.payments[payment_id] = pending_ticket(payment_id=payment_id, expires_at=expires)


async def test_the_first_ticket_holds_until_its_own_expiry() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(), confirmations=confirmations)

    await world.handler(message())

    assert [hold[1] for hold in confirmations.holds] == [EXPIRES + TICKET_GRACE]


async def test_a_later_ticket_is_capped_at_the_first_tickets_expiry() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    world.clock.current = NOW + timedelta(hours=40)
    second_ticket(world, NOW + timedelta(hours=88))

    await world.handler(message("999"))

    assert [hold[1] for hold in confirmations.holds] == [
        EXPIRES + TICKET_GRACE,
        EXPIRES + TICKET_GRACE,
    ]
    assert world.payments.by_id["999"].held_at == NOW + timedelta(hours=40)
    assert world.payments.by_id["999"].expires_at == NOW + timedelta(hours=88)  # still recorded


async def test_a_second_ticket_expiring_earlier_is_not_raised_to_the_first() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    earlier = EXPIRES - timedelta(hours=10)
    second_ticket(world, earlier)

    await world.handler(message("999"))

    assert [hold[1] for hold in confirmations.holds] == [
        EXPIRES + TICKET_GRACE,
        earlier + TICKET_GRACE,
    ]


async def test_a_third_ticket_is_capped_at_the_first_not_the_second() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    world.clock.current = NOW + timedelta(hours=1)
    second_ticket(world, EXPIRES - timedelta(hours=10), "222")
    await world.handler(message("222"))
    world.clock.current = NOW + timedelta(hours=2)
    second_ticket(world, EXPIRES + timedelta(hours=30), "333")

    await world.handler(message("333"))

    assert confirmations.holds[2][1] == EXPIRES + TICKET_GRACE


async def test_a_ticket_of_another_order_does_not_cap_this_one() -> None:
    confirmations = HoldingConfirmations()
    world = World(
        fetched=pending_ticket(),
        confirmations=confirmations,
        stored=(
            ticket(
                payment_id="foreign",
                order_id=OTHER,
                held_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            ),
        ),
    )

    await world.handler(message())

    assert [hold[1] for hold in confirmations.holds] == [EXPIRES + TICKET_GRACE]


async def test_the_ticket_being_processed_is_never_its_own_first() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(), confirmations=confirmations)

    await world.handler(message())
    world.gateway.payments[PAYMENT_ID] = pending_ticket(expires_at=EXPIRES + timedelta(hours=5))
    await world.handler(message())

    assert len(confirmations.holds) == 1  # held once; the refreshed expiry changes nothing


async def test_an_expired_on_arrival_ticket_never_caps_a_later_one() -> None:
    confirmations = HoldingConfirmations()
    gone = pending_ticket(payment_id="gone", expires_at=NOW - timedelta(minutes=1))
    world = World(fetched=gone, confirmations=confirmations)
    world.gateway.payments["gone"] = gone
    await world.handler(message("gone"))
    world.gateway.payments[PAYMENT_ID] = pending_ticket()

    await world.handler(message())

    assert [hold[1] for hold in confirmations.holds] == [EXPIRES + TICKET_GRACE]


# --- L1: expired on arrival --------------------------------------------------------------------


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(seconds=1), -timedelta(days=2)])
async def test_a_ticket_already_expired_is_marked_held_with_no_hold_and_logged(
    offset: timedelta,
) -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(expires_at=NOW + offset), confirmations=confirmations)

    with capture_logs() as logs:
        await world.handler(message())

    assert confirmations.holds == []
    assert world.row().held_at == NOW
    assert (world.row().status, world.row().outcome) == ("pending", None)
    [log] = [e for e in logs if e["event"] == "payments.ticket_expired_on_arrival"]
    assert log["payment_id"] == PAYMENT_ID


async def test_a_redelivered_expired_ticket_is_not_processed_again() -> None:
    confirmations = HoldingConfirmations()
    world = World(
        fetched=pending_ticket(expires_at=NOW - timedelta(hours=1)), confirmations=confirmations
    )
    await world.handler(message())

    with capture_logs() as logs:
        world.clock.current = NOW + timedelta(minutes=5)
        await world.handler(message())

    assert world.row().held_at == NOW
    assert [e for e in logs if e["event"] == "payments.ticket_expired_on_arrival"] == []
    assert confirmations.holds == []


async def test_a_ticket_expiring_one_second_after_now_is_still_held() -> None:
    confirmations = HoldingConfirmations()
    world = World(
        fetched=pending_ticket(expires_at=NOW + timedelta(seconds=1)), confirmations=confirmations
    )

    await world.handler(message())

    assert len(confirmations.holds) == 1


# --- decision 20: CloseCheckout cancels the pending tickets ------------------------------------


def cancelling(
    gateway: FakeGateway, payments: InMemoryPayments, checkouts: InMemoryCheckouts | None = None
) -> CloseCheckout:
    return CloseCheckout(
        gateway=gateway,
        checkouts=checkouts or InMemoryCheckouts(),
        payments=payments,
        transactions=InMemoryTransactionRunner(),
        clock=FixedClock(NOW),
    )


async def test_pending_tickets_are_cancelled_after_the_preference_is_closed() -> None:
    gateway = FakeGateway()
    payments = InMemoryPayments(
        ticket(payment_id="b", created_at=NOW),
        ticket(payment_id="a", created_at=NOW - timedelta(hours=1), payment_type="bank_transfer"),
    )
    checkouts = InMemoryCheckouts(make_checkout(NOW + timedelta(hours=40), preference_id="pref-7"))

    await cancelling(gateway, payments, checkouts)(close_requested())

    assert gateway.closed == [("pref-7", NOW)]
    assert gateway.cancelled == ["a", "b"]
    assert checkouts.by_order[ORDER_ID].expires_at == NOW


async def test_tickets_are_cancelled_even_when_the_order_has_no_stored_checkout() -> None:
    gateway = FakeGateway()

    await cancelling(gateway, InMemoryPayments(ticket(payment_id="a")))(close_requested())

    assert gateway.closed == []
    assert gateway.cancelled == ["a"]


async def test_an_expired_pending_ticket_is_cancelled_too() -> None:
    gateway = FakeGateway()
    payments = InMemoryPayments(ticket(payment_id="a", expires_at=NOW - timedelta(days=1)))

    await cancelling(gateway, payments)(close_requested())

    assert gateway.cancelled == ["a"]


async def test_only_pending_cash_tickets_of_the_order_are_cancelled() -> None:
    gateway = FakeGateway()
    payments = InMemoryPayments(
        ticket(payment_id="mine"),
        ticket(payment_id="paid", status="approved"),
        ticket(payment_id="card", payment_type="credit_card"),
        ticket(payment_id="theirs", order_id=OTHER),
    )

    await cancelling(gateway, payments)(close_requested())

    assert gateway.cancelled == ["mine"]


async def test_no_checkout_and_no_tickets_calls_nothing() -> None:
    gateway = FakeGateway()

    await cancelling(gateway, InMemoryPayments())(close_requested())

    assert (gateway.closed, gateway.cancelled) == ([], [])


async def test_a_refused_cancel_goes_on_with_the_next_ticket_and_edits_no_row() -> None:
    gateway = FakeGateway()
    gateway.refused_cancels.add("a")
    payments = InMemoryPayments(
        ticket(payment_id="a", created_at=NOW - timedelta(hours=1)),
        ticket(payment_id="b"),
    )

    await cancelling(gateway, payments)(close_requested())

    assert gateway.cancelled == ["a", "b"]
    assert {p.status for p in payments.by_id.values()} == {"pending"}


async def test_a_gateway_error_on_a_cancel_raises_so_the_event_retries() -> None:
    gateway = FakeGateway(fail=True)

    with pytest.raises(RuntimeError, match=r"^payments\.gateway_unavailable$"):
        await cancelling(gateway, InMemoryPayments(ticket(payment_id="a")))(close_requested())


async def test_a_gateway_error_on_the_close_raises_before_any_ticket_is_cancelled() -> None:
    gateway = FakeGateway(fail=True)
    checkouts = InMemoryCheckouts(make_checkout(NOW + timedelta(hours=1)))

    with pytest.raises(RuntimeError, match=r"^payments\.gateway_unavailable$"):
        await cancelling(gateway, InMemoryPayments(ticket(payment_id="a")), checkouts)(
            close_requested()
        )

    assert gateway.cancelled == []
