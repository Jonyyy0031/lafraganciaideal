"""Plan 002 step 4, application: `ProcessPaymentNotification` (process_payment.py) over fakes.

Every row of the plan's outcome table, idempotent redelivery, the raise paths (the relay rolls back
and retries) and the facts that keep the handler honest: the gateway is fetched outside the
transaction, an outcome is never set twice and the flag event carries the payment's own amount."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.handlers.process_payment import (
    ProcessPaymentNotification,
)
from fragancia_api.modules.payments.application.ports import Confirmation, GatewayPayment
from fragancia_api.modules.payments.domain.events import PaymentFlagged
from fragancia_api.modules.payments.domain.payment import Payment, PaymentOutcome
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway, InMemoryPayments
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.payments.support import NOW

PAYMENT_ID = "555000111"
ORDER_ID = UUID(int=900)


def gateway_payment(**overrides: object) -> GatewayPayment:
    values: dict[str, object] = {
        "payment_id": PAYMENT_ID,
        "status": "approved",
        "status_detail": "accredited",
        "external_reference": str(ORDER_ID),
        "amount_cents": 268_000,
        "currency": "MXN",
        "payment_type": "credit_card",
        "approved_at": NOW - timedelta(seconds=30),
    }
    values.update(overrides)
    return GatewayPayment(**values)  # type: ignore[arg-type]


def message(payment_id: str = PAYMENT_ID) -> EventMessage:
    return EventMessage(
        id=UUID(int=1),
        name="payments.notification.received",
        payload={"payment_id": payment_id},
        occurred_at=NOW,
    )


class FakeConfirmations:
    """`OrderConfirmations` answering the configured outcome (or error) and recording the calls."""

    def __init__(
        self, outcome: str = "paid", number: int | None = 1001, error: DomainError | None = None
    ) -> None:
        self.outcome = outcome
        self.number = number
        self.error = error
        self.calls: list[tuple[UUID, str, int, str]] = []

    async def confirm(
        self, order_id: UUID, payment_id: str, amount_cents: int, currency: str
    ) -> Result[Confirmation, DomainError]:
        self.calls.append((order_id, payment_id, amount_cents, currency))
        if self.error is not None:
            return Err(self.error)
        return Ok(Confirmation(outcome=self.outcome, number=self.number))

    async def hold(
        self,
        order_id: UUID,
        until: datetime,
        pay_by: datetime,
        method: str,
        instructions_url: str | None,
    ) -> Result[Confirmation, DomainError]:
        """Plan 003's port member; these plan 002 tests never reach it (no pending tickets)."""
        raise AssertionError("hold is not expected in these tests")


class TrackingRunner(InMemoryTransactionRunner):
    """Tells whether the code runs inside a transaction."""

    def __init__(self) -> None:
        self.inside = False
        self.runs = 0

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        self.runs += 1
        self.inside = True
        try:
            return await work()
        finally:
            self.inside = False


class WatchedGateway(FakeGateway):
    def __init__(self, runner: TrackingRunner, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._runner = runner
        self.fetched_inside: list[bool] = []

    async def get_payment(self, payment_id: str):  # type: ignore[no-untyped-def]
        self.fetched_inside.append(self._runner.inside)
        return await super().get_payment(payment_id)


@dataclass
class World:
    fetched: GatewayPayment | None = field(default_factory=gateway_payment)
    confirmations: FakeConfirmations = field(default_factory=FakeConfirmations)
    stored: tuple[Payment, ...] = ()

    def __post_init__(self) -> None:
        self.runner = TrackingRunner()
        self.gateway = WatchedGateway(
            self.runner, payments={PAYMENT_ID: self.fetched} if self.fetched else {}
        )
        self.payments = InMemoryPayments(*self.stored)
        self.events = RecordingEventPublisher()
        self.clock = FixedClock(NOW)

    @property
    def handler(self) -> ProcessPaymentNotification:
        return ProcessPaymentNotification(
            gateway=self.gateway,
            payments=self.payments,
            orders=self.confirmations,
            transactions=self.runner,
            events=self.events,
            clock=self.clock,
        )

    def row(self) -> Payment:
        return self.payments.by_id[PAYMENT_ID]

    def flags(self) -> list[dict[str, object]]:
        assert all(isinstance(e, PaymentFlagged) for e in self.events.published)
        return [e.payload() for e in self.events.published]


# --- the outcome table -------------------------------------------------------------------------


async def test_an_approved_payment_that_pays_the_order_is_applied() -> None:
    world = World()

    await world.handler(message())

    assert world.confirmations.calls == [(ORDER_ID, PAYMENT_ID, 268_000, "MXN")]
    stored = world.row()
    assert stored.outcome is PaymentOutcome.APPLIED
    assert (stored.status, stored.status_detail) == ("approved", "accredited")
    assert (stored.order_id, stored.amount_cents, stored.currency) == (ORDER_ID, 268_000, "MXN")
    assert (stored.payment_type, stored.approved_at) == ("credit_card", NOW - timedelta(seconds=30))
    assert (stored.created_at, stored.updated_at) == (NOW, NOW)
    assert world.flags() == []


async def test_an_approved_payment_for_an_order_that_is_not_pending_is_refund_due_unflagged() -> (
    None
):
    world = World(confirmations=FakeConfirmations("not_pending"))

    await world.handler(message())

    assert world.row().outcome is PaymentOutcome.REFUND_DUE
    assert world.flags() == []


async def test_an_approved_payment_when_the_units_are_gone_is_refund_due_and_flagged() -> None:
    world = World(confirmations=FakeConfirmations("stock_gone", number=1007))

    await world.handler(message())

    assert world.row().outcome is PaymentOutcome.REFUND_DUE
    assert world.flags() == [
        {
            "payment_id": PAYMENT_ID,
            "number": 1007,
            "reason": "stock_gone",
            "amount_cents": 268_000,
            "currency": "MXN",
        }
    ]


async def test_an_approved_payment_with_another_amount_is_amount_mismatch_and_flagged() -> None:
    world = World(
        fetched=gateway_payment(amount_cents=100, currency="USD"),
        confirmations=FakeConfirmations("amount_mismatch", number=1008),
    )

    await world.handler(message())

    assert world.confirmations.calls == [(ORDER_ID, PAYMENT_ID, 100, "USD")]
    assert world.row().outcome is PaymentOutcome.AMOUNT_MISMATCH
    assert world.flags() == [
        {
            "payment_id": PAYMENT_ID,
            "number": 1008,
            "reason": "amount_mismatch",
            "amount_cents": 100,
            "currency": "USD",
        }
    ]


async def test_an_approved_payment_for_an_unknown_order_keeps_its_outcome_unset_and_logs() -> None:
    world = World(confirmations=FakeConfirmations("not_found", number=None))

    with capture_logs() as logs:
        await world.handler(message())

    stored = world.row()
    assert stored.outcome is None
    assert stored.status == "approved"
    assert world.flags() == []
    assert [entry["event"] for entry in logs] == ["payments.payment_unmatched"]


@pytest.mark.parametrize(
    "status",
    ["pending", "in_process", "authorized", "rejected", "cancelled", "refunded", "charged_back"],
)
async def test_any_other_status_is_recorded_and_the_order_is_never_touched(status: str) -> None:
    world = World(fetched=gateway_payment(status=status, status_detail="detail", approved_at=None))

    await world.handler(message())

    stored = world.row()
    assert (stored.status, stored.status_detail, stored.approved_at) == (status, "detail", None)
    assert stored.outcome is None
    assert world.confirmations.calls == []
    assert world.flags() == []


async def test_a_payment_that_was_pending_and_is_now_approved_is_applied() -> None:
    world = World(fetched=gateway_payment(status="pending", approved_at=None))
    await world.handler(message())
    assert world.row().outcome is None

    world.gateway.payments[PAYMENT_ID] = gateway_payment()
    world.clock.current = NOW + timedelta(minutes=2)
    await world.handler(message())

    stored = world.row()
    assert stored.outcome is PaymentOutcome.APPLIED
    assert stored.status == "approved"
    assert (stored.created_at, stored.updated_at) == (NOW, NOW + timedelta(minutes=2))
    assert len(world.confirmations.calls) == 1


# --- idempotency -------------------------------------------------------------------------------


async def test_a_redelivered_notification_changes_nothing_and_confirms_once() -> None:
    world = World(confirmations=FakeConfirmations("stock_gone", number=1007))
    await world.handler(message())
    first = world.row()

    world.clock.current = NOW + timedelta(minutes=5)
    await world.handler(message())

    assert len(world.confirmations.calls) == 1
    assert len(world.flags()) == 1
    second = world.row()
    assert second.outcome is first.outcome is PaymentOutcome.REFUND_DUE
    assert second.status == first.status


async def test_a_payment_stored_with_an_outcome_is_never_confirmed_again() -> None:
    stored = Payment(
        payment_id=PAYMENT_ID,
        order_id=ORDER_ID,
        status="approved",
        status_detail="accredited",
        amount_cents=268_000,
        currency="MXN",
        payment_type="credit_card",
        approved_at=NOW,
        outcome=PaymentOutcome.APPLIED,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    world = World(stored=(stored,))

    await world.handler(message())

    assert world.confirmations.calls == []
    assert world.row().outcome is PaymentOutcome.APPLIED
    assert world.row().created_at == NOW - timedelta(days=1)
    assert world.row().updated_at == NOW


async def test_a_later_refund_updates_the_status_and_keeps_the_applied_outcome() -> None:
    world = World()
    await world.handler(message())
    world.gateway.payments[PAYMENT_ID] = gateway_payment(
        status="refunded", status_detail="refunded"
    )

    await world.handler(message())

    stored = world.row()
    assert (stored.status, stored.outcome) == ("refunded", PaymentOutcome.APPLIED)
    assert len(world.confirmations.calls) == 1


async def test_a_refresh_does_not_replace_the_amount_or_the_order_stored_first() -> None:
    world = World()
    await world.handler(message())
    world.gateway.payments[PAYMENT_ID] = gateway_payment(
        amount_cents=1, external_reference=str(UUID(int=5))
    )

    await world.handler(message())

    stored = world.row()
    assert (stored.amount_cents, stored.order_id) == (268_000, ORDER_ID)


async def test_the_payment_id_comes_from_the_event_payload() -> None:
    world = World()

    await world.handler(message(PAYMENT_ID))

    assert world.gateway.fetched == [PAYMENT_ID]


# --- nothing to apply --------------------------------------------------------------------------


async def test_a_payment_mercado_pago_does_not_know_is_logged_and_ignored() -> None:
    world = World(fetched=None)

    with capture_logs() as logs:
        await world.handler(message())

    assert world.payments.by_id == {}
    assert world.confirmations.calls == []
    assert [(e["event"], e["log_level"]) for e in logs] == [
        ("payments.payment_not_found", "warning")
    ]
    assert world.runner.runs == 0


@pytest.mark.parametrize("reference", [None, "", "not-a-uuid", "12345", "LFI-1001"])
async def test_a_payment_whose_reference_is_not_an_order_id_is_logged_and_ignored(
    reference: str | None,
) -> None:
    world = World(fetched=gateway_payment(external_reference=reference))

    with capture_logs() as logs:
        await world.handler(message())

    assert world.payments.by_id == {}
    assert world.confirmations.calls == []
    assert [e["event"] for e in logs] == ["payments.payment_unmatched"]


# --- failures that retry -----------------------------------------------------------------------


async def test_mercado_pago_down_raises_so_the_relay_retries_and_stores_nothing() -> None:
    world = World()
    world.gateway.fail = True

    with pytest.raises(RuntimeError, match=r"^payments\.gateway_unavailable$"):
        await world.handler(message())

    assert world.payments.by_id == {}
    assert world.confirmations.calls == []
    assert world.flags() == []
    assert world.runner.runs == 0


async def test_a_refusal_from_orders_raises_with_its_code_and_saves_nothing() -> None:
    refusal = ConflictError(code="INVENTORY_RESERVATION_COMMITTED", message="refused")
    world = World(confirmations=FakeConfirmations(error=refusal))

    with pytest.raises(RuntimeError, match=r"^payments\.confirm_refused: INVENTORY_RESERVATION"):
        await world.handler(message())

    # `claim` stored the row before confirming; nothing was applied to it. Over PostgreSQL the
    # raise rolls the row back too (integration `test_payment_confirmation_flow.py`).
    assert [p.outcome for p in world.payments.by_id.values()] == [None]
    assert world.flags() == []


async def test_a_flag_without_the_order_number_raises_instead_of_emailing_nothing() -> None:
    world = World(confirmations=FakeConfirmations("stock_gone", number=None))

    with pytest.raises(ValueError, match="needs the order number"):
        await world.handler(message())

    assert world.flags() == []


# --- transaction shape -------------------------------------------------------------------------


async def test_the_payment_is_fetched_outside_the_transaction_and_applied_inside_it() -> None:
    world = World()
    seen_inside: list[bool] = []
    original = world.confirmations.confirm

    async def watching(*args: object):  # type: ignore[no-untyped-def]
        seen_inside.append(world.runner.inside)
        return await original(*args)  # type: ignore[arg-type]

    world.confirmations.confirm = watching  # type: ignore[method-assign, assignment]

    await world.handler(message())

    assert world.gateway.fetched_inside == [False]
    assert seen_inside == [True]
    assert world.runner.runs == 1


# --- `InMemoryPayments.claim` (plan 002 step 7, H1) ---------------------------------------------


def stored_payment(**overrides: object) -> Payment:
    values: dict[str, object] = {
        "payment_id": PAYMENT_ID,
        "order_id": ORDER_ID,
        "status": "approved",
        "status_detail": "accredited",
        "amount_cents": 268_000,
        "currency": "MXN",
        "payment_type": "credit_card",
        "approved_at": NOW,
        "outcome": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Payment(**values)  # type: ignore[arg-type]


async def test_claim_stores_an_unknown_payment_and_returns_it() -> None:
    payments = InMemoryPayments()
    fresh = stored_payment()

    claimed = await payments.claim(fresh)

    assert claimed == fresh
    assert payments.by_id[PAYMENT_ID] == fresh


async def test_claim_keeps_the_stored_row_and_its_outcome_over_the_one_offered() -> None:
    payments = InMemoryPayments(stored_payment(outcome=PaymentOutcome.APPLIED))

    claimed = await payments.claim(stored_payment(status="pending", outcome=None))

    assert claimed.outcome is PaymentOutcome.APPLIED
    assert claimed.status == "approved"
    assert payments.by_id[PAYMENT_ID].status == "approved"


async def test_claim_hands_out_its_own_copy() -> None:
    payments = InMemoryPayments()

    claimed = await payments.claim(stored_payment())
    claimed.settle(PaymentOutcome.APPLIED, NOW)

    assert payments.by_id[PAYMENT_ID].outcome is None
