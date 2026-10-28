"""Plan 003 step 4, application: `ProcessPaymentNotification` with a pending cash or SPEI ticket
(process_payment.py): the hold of the order once per payment, what each answer of orders does to
`held_at`, a refusal that retries, re-delivery, and an approved payment after the hold. Over the
fakes of the plan 002 tests; the real transaction is in the integration tests."""

from datetime import datetime, timedelta
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.ports import Confirmation
from fragancia_api.modules.payments.domain.payment import TICKET_GRACE, PaymentOutcome
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.payments.support import NOW
from tests.unit.payments.test_process_payment import (
    ORDER_ID,
    PAYMENT_ID,
    FakeConfirmations,
    World,
    gateway_payment,
    message,
)

EXPIRES = NOW + timedelta(hours=48)
URL = "https://mp.test/ticket/555000111"


class HoldingConfirmations(FakeConfirmations):
    """`hold` answers the configured outcome (or error) and records its calls."""

    def __init__(
        self,
        hold_outcome: str = "held",
        hold_error: DomainError | None = None,
    ) -> None:
        super().__init__()
        self.hold_outcome = hold_outcome
        self.hold_error = hold_error
        self.holds: list[tuple[UUID, datetime, str, str | None]] = []

    async def hold(
        self,
        order_id: UUID,
        until: datetime,
        pay_by: datetime,
        method: str,
        instructions_url: str | None,
    ) -> Result[Confirmation, DomainError]:
        self.holds.append((order_id, until, method, instructions_url))
        if self.hold_error is not None:
            return Err(self.hold_error)
        return Ok(Confirmation(outcome=self.hold_outcome, number=self.number))


def pending_ticket(**overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "status": "pending",
        "status_detail": "pending_waiting_payment",
        "approved_at": None,
        "payment_type": "ticket",
        "payment_method": "oxxo",
        "expires_at": EXPIRES,
        "ticket_url": URL,
    }
    values.update(overrides)
    return gateway_payment(**values)


def ticket_world(
    hold_outcome: str = "held", hold_error: DomainError | None = None
) -> tuple[World, HoldingConfirmations]:
    confirmations = HoldingConfirmations(hold_outcome, hold_error)
    return World(fetched=pending_ticket(), confirmations=confirmations), confirmations


async def test_a_pending_oxxo_ticket_holds_the_order_until_it_expires() -> None:
    world, confirmations = ticket_world()

    await world.handler(message())

    assert confirmations.holds == [(ORDER_ID, EXPIRES + TICKET_GRACE, "efectivo en tienda", URL)]
    assert confirmations.calls == []
    stored = world.row()
    assert stored.held_at == NOW
    assert (stored.payment_method, stored.expires_at, stored.ticket_url) == ("oxxo", EXPIRES, URL)
    assert (stored.status, stored.outcome) == ("pending", None)
    assert world.flags() == []


async def test_a_pending_spei_transfer_holds_the_order_with_its_own_label() -> None:
    confirmations = HoldingConfirmations()
    world = World(
        fetched=pending_ticket(payment_type="bank_transfer", payment_method="clabe"),
        confirmations=confirmations,
    )

    await world.handler(message())

    assert confirmations.holds == [(ORDER_ID, EXPIRES + TICKET_GRACE, "transferencia SPEI", URL)]
    assert world.row().held_at == NOW


async def test_a_ticket_without_a_page_holds_with_no_instructions_url() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(ticket_url=None), confirmations=confirmations)

    await world.handler(message())

    assert confirmations.holds == [(ORDER_ID, EXPIRES + TICKET_GRACE, "efectivo en tienda", None)]


async def test_a_redelivered_notification_holds_once() -> None:
    world, confirmations = ticket_world()

    await world.handler(message())
    world.clock.current = NOW + timedelta(minutes=5)
    await world.handler(message())

    assert len(confirmations.holds) == 1
    assert world.row().held_at == NOW


async def test_each_payment_of_the_order_holds_it_once() -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(), confirmations=confirmations)
    await world.handler(message())
    world.gateway.payments["999"] = pending_ticket(
        payment_id="999", expires_at=EXPIRES + timedelta(hours=1)
    )

    await world.handler(message("999"))

    # Decision 19 (plan 003 step 8): the second hold never goes past the first ticket's expiry.
    assert [hold[1] for hold in confirmations.holds] == [
        EXPIRES + TICKET_GRACE,
        EXPIRES + TICKET_GRACE,
    ]


@pytest.mark.parametrize("outcome", ["not_pending", "not_found"])
async def test_a_ticket_for_a_paid_cancelled_or_unknown_order_is_marked_held_and_logged(
    outcome: str,
) -> None:
    world, confirmations = ticket_world(hold_outcome=outcome)

    with capture_logs() as logs:
        await world.handler(message())
    await world.handler(message())

    assert world.row().held_at == NOW
    assert len(confirmations.holds) == 1  # never retried
    [log] = [entry for entry in logs if entry["event"] == "payments.ticket_not_held"]
    assert (log["payment_id"], log["outcome"]) == (PAYMENT_ID, outcome)


async def test_a_refusal_from_orders_raises_so_the_relay_retries_and_the_payment_is_not_held() -> (
    None
):
    refusal = ConflictError(code="ORDERS_TRANSITION_INVALID", message="refused")
    world, confirmations = ticket_world(hold_error=refusal)

    with pytest.raises(RuntimeError, match=r"^payments\.confirm_refused: ORDERS_TRANSITION"):
        await world.handler(message())

    assert world.row().held_at is None

    confirmations.hold_error = None
    await world.handler(message())

    assert len(confirmations.holds) == 2
    assert world.row().held_at == NOW


@pytest.mark.parametrize(
    "overrides",
    [
        {"payment_type": "credit_card", "payment_method": "visa", "expires_at": None},
        {"payment_type": "credit_card"},
        {"expires_at": None},
        {"status": "cancelled"},
        {"status": "expired"},
        {"payment_type": "atm"},
        {"payment_type": None},
    ],
)
async def test_a_payment_that_is_not_a_pending_ticket_with_an_expiry_never_holds(
    overrides: dict[str, object],
) -> None:
    confirmations = HoldingConfirmations()
    world = World(fetched=pending_ticket(**overrides), confirmations=confirmations)

    await world.handler(message())

    assert confirmations.holds == []
    assert world.row().held_at is None


async def test_the_ticket_paid_later_is_applied_as_any_approved_payment_and_holds_no_more() -> None:
    world, confirmations = ticket_world()
    await world.handler(message())
    world.gateway.payments[PAYMENT_ID] = pending_ticket(
        status="approved", status_detail="accredited", approved_at=NOW + timedelta(hours=3)
    )
    world.clock.current = NOW + timedelta(hours=3)

    await world.handler(message())

    assert len(confirmations.holds) == 1
    assert confirmations.calls == [(ORDER_ID, PAYMENT_ID, 268_000, "MXN")]
    stored = world.row()
    assert (stored.status, stored.outcome) == ("approved", PaymentOutcome.APPLIED)
    assert stored.held_at == NOW


async def test_a_ticket_approved_on_the_first_delivery_is_applied_and_never_held() -> None:
    confirmations = HoldingConfirmations()
    world = World(
        fetched=pending_ticket(status="approved", approved_at=NOW), confirmations=confirmations
    )

    await world.handler(message())

    assert confirmations.holds == []
    assert world.row().outcome is PaymentOutcome.APPLIED


async def test_a_later_delivery_updates_the_page_and_the_expiry_without_holding_again() -> None:
    world, confirmations = ticket_world()
    await world.handler(message())
    later = EXPIRES + timedelta(hours=2)
    world.gateway.payments[PAYMENT_ID] = pending_ticket(
        expires_at=later, ticket_url="https://mp.test/new"
    )

    await world.handler(message())

    assert len(confirmations.holds) == 1
    stored = world.row()
    assert (stored.expires_at, stored.ticket_url) == (later, "https://mp.test/new")
    assert stored.held_at == NOW


async def test_the_hold_runs_inside_the_transaction() -> None:
    world, confirmations = ticket_world()
    seen_inside: list[bool] = []
    original = confirmations.hold

    async def watching(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        seen_inside.append(world.runner.inside)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    confirmations.hold = watching  # type: ignore[method-assign]

    await world.handler(message())

    assert world.gateway.fetched_inside == [False]
    assert seen_inside == [True]
