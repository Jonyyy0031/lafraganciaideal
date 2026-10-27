"""Payments plan 003 step 8 (review repair) on PostgreSQL (`fragancia_test`):
`SqlPaymentRepository.first_hold_expiry` and `pending_cash`, and `CloseCheckout` cancelling the
order's pending tickets over the real repositories."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.payments.application.handlers.close_checkout import CloseCheckout
from fragancia_api.modules.payments.domain.payment import Payment
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.modules.payments.infrastructure.sql_checkout_repository import (
    SqlCheckoutRepository,
)
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import (
    SqlPaymentRepository,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import row
from tests.integration.payments.test_sql_payments import get, put
from tests.integration.payments.test_sql_tickets import (
    NOW,
    ORDER,
    OTHER,
    make_checkout,
    make_ticket,
)

pytestmark = pytest.mark.integration

LATER = NOW + timedelta(hours=48)


async def first_hold(container: Container, order_id: UUID = ORDER) -> datetime | None:
    repo = SqlPaymentRepository(container.database)
    return await in_transaction(container, lambda: repo.first_hold_expiry(order_id))


async def pending_cash(container: Container, order_id: UUID = ORDER) -> list[Payment]:
    repo = SqlPaymentRepository(container.database)
    return await in_transaction(container, lambda: repo.pending_cash(order_id))


def close_requested() -> EventMessage:
    return EventMessage(
        id=UUID(int=6),
        name="payments.checkout.close_requested",
        payload={"order_id": str(ORDER)},
        occurred_at=NOW,
    )


def closer(container: Container, gateway: FakeGateway) -> CloseCheckout:
    return CloseCheckout(
        gateway=gateway,
        checkouts=SqlCheckoutRepository(container.database),
        payments=SqlPaymentRepository(container.database),
        transactions=SqlTransactionRunner(container.database),
        clock=SystemClock(),
    )


# --- first_hold_expiry -------------------------------------------------------------------------


async def test_first_hold_expiry_is_none_without_a_held_payment(container: Container) -> None:
    await put(container, make_ticket("a"))

    assert await first_hold(container) is None


async def test_first_hold_expiry_is_the_expiry_of_the_earliest_held_payment(
    container: Container,
) -> None:
    await put(
        container,
        make_ticket("late", held_at=NOW + timedelta(hours=1), expires_at=NOW + timedelta(hours=90)),
    )
    await put(container, make_ticket("early", held_at=NOW, expires_at=LATER))
    await put(container, make_ticket("unheld", expires_at=NOW + timedelta(hours=2)))

    assert await first_hold(container) == LATER


async def test_first_hold_expiry_breaks_a_tie_by_payment_id(container: Container) -> None:
    await put(container, make_ticket("b", held_at=NOW, expires_at=NOW + timedelta(hours=60)))
    await put(container, make_ticket("a", held_at=NOW, expires_at=LATER))

    assert await first_hold(container) == LATER


async def test_first_hold_expiry_ignores_an_expired_on_arrival_ticket(container: Container) -> None:
    await put(container, make_ticket("gone", held_at=NOW, expires_at=NOW - timedelta(hours=1)))
    await put(container, make_ticket("equal", held_at=NOW, expires_at=NOW))
    assert await first_hold(container) is None

    await put(container, make_ticket("real", held_at=NOW + timedelta(hours=1), expires_at=LATER))

    assert await first_hold(container) == LATER


async def test_first_hold_expiry_counts_only_the_asked_order(container: Container) -> None:
    await put(container, make_ticket("mine", held_at=NOW, order_id=OTHER))

    assert await first_hold(container) is None
    assert await first_hold(container, OTHER) == LATER


# --- pending_cash ------------------------------------------------------------------------------


async def test_pending_cash_lists_pending_tickets_whatever_their_expiry_oldest_first(
    container: Container,
) -> None:
    await put(container, make_ticket("new", created_at=NOW))
    await put(
        container,
        make_ticket("old", created_at=NOW - timedelta(hours=1), payment_type="bank_transfer"),
    )
    await put(
        container,
        make_ticket(
            "past", created_at=NOW - timedelta(hours=2), expires_at=NOW - timedelta(days=1)
        ),
    )
    await put(container, make_ticket("same-b", created_at=NOW + timedelta(hours=1)))
    await put(container, make_ticket("same-a", created_at=NOW + timedelta(hours=1)))

    found = await pending_cash(container)

    assert [p.payment_id for p in found] == ["past", "old", "new", "same-a", "same-b"]
    assert found[1] == await get(container, "old")


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "cancelled"},
        {"payment_type": "credit_card"},
        {"payment_type": "atm"},
        {"payment_type": None},
        {"order_id": None},
        {"order_id": OTHER},
    ],
)
async def test_pending_cash_leaves_out_everything_else(
    container: Container, overrides: dict[str, Any]
) -> None:
    await put(container, make_ticket(**overrides))

    assert await pending_cash(container) == []


# --- CloseCheckout over SQL --------------------------------------------------------------------


async def test_closing_cancels_the_pending_tickets_and_edits_no_row(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)
    await in_transaction(container, lambda: repo.upsert(make_checkout()))
    await put(container, make_ticket("a", created_at=NOW - timedelta(hours=1)))
    await put(container, make_ticket("b", payment_type="bank_transfer"))
    await put(container, make_ticket("paid", status="approved"))
    await put(container, make_ticket("theirs", order_id=OTHER))
    gateway = FakeGateway()

    await closer(container, gateway)(close_requested())

    assert gateway.cancelled == ["a", "b"]
    assert [preference for preference, _ in gateway.closed] == ["pref-900"]
    pending = await row(
        container, "SELECT count(*) AS n FROM payments.payments WHERE status = 'pending'"
    )
    # No row is edited here: Mercado Pago's notification of the cancelled status refreshes them.
    assert pending["n"] == 3


async def test_closing_cancels_tickets_when_the_order_has_no_stored_checkout(
    container: Container,
) -> None:
    await put(container, make_ticket("a"))
    gateway = FakeGateway()

    await closer(container, gateway)(close_requested())

    assert gateway.closed == []
    assert gateway.cancelled == ["a"]


async def test_a_gateway_error_on_a_cancel_raises_so_the_event_retries(
    container: Container,
) -> None:
    await put(container, make_ticket("a"))

    with pytest.raises(RuntimeError, match="payments.gateway_unavailable"):
        await closer(container, FakeGateway(fail=True))(close_requested())
