"""Payments plan 003 on PostgreSQL (`fragancia_test`): the ticket columns of
`SqlPaymentRepository` (read back, upsert, `held_at` kept across a claim), `SqlPaymentQueries
.pending_for_order` (which payments, the expiry boundary, the order, one order only),
`SqlCheckoutRepository.close`, and the close of the Mercado Pago link end to end over the real
outbox and checkouts."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.payments.application.handlers.close_checkout import (
    CloseCheckout,
    RequestCheckoutClose,
)
from fragancia_api.modules.payments.contracts import PendingTicket
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.domain.payment import Payment
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.modules.payments.infrastructure.sql_checkout_repository import (
    SqlCheckoutRepository,
)
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import (
    SqlPaymentQueries,
    SqlPaymentRepository,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher
from tests.integration.inventory.test_stock_status_flow import outbox_rows
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import row
from tests.integration.payments.test_sql_payments import get, make_payment, put

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ORDER = UUID(int=900)
OTHER = UUID(int=901)


def make_ticket(payment_id: str = "t1", **overrides: object) -> Payment:
    values: dict[str, object] = {
        "status": "pending",
        "status_detail": "pending_waiting_payment",
        "approved_at": None,
        "payment_type": "ticket",
        "payment_method": "oxxo",
        "expires_at": NOW + timedelta(hours=48),
        "ticket_url": f"https://mp.test/{payment_id}",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return make_payment(payment_id, **values)


async def pending(container: Container, order_id: UUID = ORDER) -> list[PendingTicket]:
    return await SqlPaymentQueries(container.database).pending_for_order(order_id, NOW)


# --- the repository ----------------------------------------------------------------------------


async def test_a_ticket_is_read_back_whole_with_its_four_columns(container: Container) -> None:
    ticket = make_ticket(held_at=NOW + timedelta(minutes=1))

    await put(container, ticket)

    assert await get(container, "t1") == ticket


async def test_the_ticket_columns_are_stored_as_given(container: Container) -> None:
    await put(container, make_ticket(held_at=NOW))

    stored = await row(container, "SELECT * FROM payments.payments")

    assert (stored["payment_method"], stored["ticket_url"]) == ("oxxo", "https://mp.test/t1")
    assert (stored["expires_at"], stored["held_at"]) == (NOW + timedelta(hours=48), NOW)


async def test_a_second_save_updates_the_ticket_columns(container: Container) -> None:
    await put(container, make_ticket(ticket_url=None, held_at=None))
    updated = make_ticket(
        ticket_url="https://mp.test/new",
        expires_at=NOW + timedelta(hours=60),
        held_at=NOW + timedelta(minutes=2),
    )

    await put(container, updated)

    assert await get(container, "t1") == updated


async def test_claim_returns_the_stored_ticket_columns_over_the_ones_offered(
    container: Container,
) -> None:
    stored = make_ticket(held_at=NOW)
    await put(container, stored)
    repo = SqlPaymentRepository(container.database)
    offered = make_ticket(payment_method=None, expires_at=None, ticket_url=None, held_at=None)

    claimed = await in_transaction(container, lambda: repo.claim(offered))

    assert claimed == stored


async def test_claim_of_a_new_ticket_stores_its_columns_with_no_hold(container: Container) -> None:
    repo = SqlPaymentRepository(container.database)
    fresh = make_ticket("fresh")

    claimed = await in_transaction(container, lambda: repo.claim(fresh))

    assert claimed == fresh
    assert claimed.held_at is None
    stored = await row(container, "SELECT * FROM payments.payments WHERE payment_id = 'fresh'")
    assert (stored["payment_method"], stored["held_at"]) == ("oxxo", None)


# --- pending_for_order -------------------------------------------------------------------------


async def test_a_live_ticket_is_listed_with_its_label_amount_expiry_and_page(
    container: Container,
) -> None:
    await put(container, make_ticket())

    assert await pending(container) == [
        PendingTicket(
            method="efectivo en tienda",
            amount_cents=268_000,
            expires_at=NOW + timedelta(hours=48),
            ticket_url="https://mp.test/t1",
        )
    ]


async def test_a_spei_transfer_is_listed_with_its_label_and_a_missing_page_as_none(
    container: Container,
) -> None:
    await put(container, make_ticket(payment_type="bank_transfer", ticket_url=None))

    [item] = await pending(container)

    assert (item.method, item.ticket_url) == ("transferencia SPEI", None)


async def test_the_tickets_come_newest_first_with_the_payment_id_as_tie_break(
    container: Container,
) -> None:
    await put(container, make_ticket("a", created_at=NOW - timedelta(hours=2), ticket_url="a"))
    await put(container, make_ticket("b", created_at=NOW, ticket_url="b"))
    await put(container, make_ticket("c", created_at=NOW, ticket_url="c"))
    await put(container, make_ticket("d", created_at=NOW - timedelta(hours=1), ticket_url="d"))

    assert [item.ticket_url for item in await pending(container)] == ["c", "b", "d", "a"]


async def test_an_expired_ticket_is_hidden_and_one_expiring_exactly_now_too(
    container: Container,
) -> None:
    await put(container, make_ticket("past", expires_at=NOW - timedelta(seconds=1)))
    await put(container, make_ticket("edge", expires_at=NOW))
    await put(container, make_ticket("live", expires_at=NOW + timedelta(seconds=1)))

    assert [item.ticket_url for item in await pending(container)] == ["https://mp.test/live"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "cancelled"},
        {"status": "expired"},
        {"payment_type": "credit_card"},
        {"payment_type": "atm"},
        {"payment_type": None},
        {"expires_at": None},
        {"order_id": None},
        {"order_id": OTHER},
    ],
)
async def test_other_payments_are_not_listed(
    container: Container, overrides: dict[str, Any]
) -> None:
    await put(container, make_ticket(**overrides))

    assert await pending(container) == []


async def test_a_held_ticket_is_still_listed(container: Container) -> None:
    await put(container, make_ticket(held_at=NOW))

    assert len(await pending(container)) == 1


async def test_only_the_tickets_of_the_asked_order_are_listed(container: Container) -> None:
    await put(container, make_ticket("mine"))
    await put(container, make_ticket("theirs", order_id=OTHER))

    assert [i.ticket_url for i in await pending(container)] == ["https://mp.test/mine"]
    assert [i.ticket_url for i in await pending(container, OTHER)] == ["https://mp.test/theirs"]


# --- closing the link --------------------------------------------------------------------------


def make_checkout(order_id: UUID = ORDER, expires_at: datetime | None = None) -> Checkout:
    return Checkout(
        order_id=order_id,
        preference_id=f"pref-{order_id.int}",
        redirect_url="https://pay.test/x",
        expires_at=expires_at or NOW + timedelta(hours=48),
        created_at=NOW,
    )


async def test_close_sets_only_the_expiry_of_that_orders_checkout(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)
    await in_transaction(container, lambda: repo.upsert(make_checkout(ORDER)))
    await in_transaction(container, lambda: repo.upsert(make_checkout(OTHER)))
    at = NOW + timedelta(minutes=5)

    await in_transaction(container, lambda: repo.close(ORDER, at))

    closed = await in_transaction(container, lambda: repo.get_for_order(ORDER))
    untouched = await in_transaction(container, lambda: repo.get_for_order(OTHER))
    assert closed is not None
    assert untouched is not None
    assert closed.expires_at == at
    assert (closed.preference_id, closed.redirect_url, closed.created_at) == (
        "pref-900",
        "https://pay.test/x",
        NOW,
    )
    assert untouched.expires_at == NOW + timedelta(hours=48)


async def test_a_closed_checkout_is_never_reused(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)
    due = NOW + timedelta(hours=48)
    await in_transaction(container, lambda: repo.upsert(make_checkout(expires_at=due)))
    at = NOW + timedelta(minutes=5)

    await in_transaction(container, lambda: repo.close(ORDER, at))

    closed = await in_transaction(container, lambda: repo.get_for_order(ORDER))
    assert closed is not None
    assert closed.reusable(due, NOW) is False


async def test_closing_an_order_without_a_checkout_changes_nothing(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)

    await in_transaction(container, lambda: repo.close(ORDER, NOW))

    count = await row(container, "SELECT count(*) AS n FROM payments.checkouts")
    assert count["n"] == 0


def status_changed(from_status: str, to_status: str) -> EventMessage:
    return EventMessage(
        id=UUID(int=5),
        name="orders.order.status_changed",
        payload={
            "order_id": str(ORDER),
            "number": 1001,
            "from_status": from_status,
            "to_status": to_status,
        },
        occurred_at=NOW,
    )


async def test_a_cancelled_order_publishes_the_request_and_its_handler_closes_the_checkout(
    container: Container,
) -> None:
    repo = SqlCheckoutRepository(container.database)
    await in_transaction(container, lambda: repo.upsert(make_checkout()))
    transactions = SqlTransactionRunner(container.database)
    gateway = FakeGateway()

    await RequestCheckoutClose(
        events=OutboxEventPublisher(container.database), transactions=transactions
    )(status_changed("pending_payment", "cancelled"))
    [request] = [
        r for r in await outbox_rows(container) if r["name"] == "payments.checkout.close_requested"
    ]
    assert request["payload"] == {"order_id": str(ORDER)}
    await CloseCheckout(
        gateway=gateway, checkouts=repo, transactions=transactions, clock=SystemClock()
    )(
        EventMessage(
            id=UUID(int=6),
            name="payments.checkout.close_requested",
            payload=request["payload"],
            occurred_at=NOW,
        )
    )

    [(preference, at)] = gateway.closed
    assert preference == "pref-900"
    closed = await in_transaction(container, lambda: repo.get_for_order(ORDER))
    assert closed is not None
    assert closed.expires_at == at
    assert closed.expires_at != NOW + timedelta(hours=48)


async def test_a_move_that_is_not_a_payment_or_a_cancel_publishes_nothing(
    container: Container,
) -> None:
    await RequestCheckoutClose(
        events=OutboxEventPublisher(container.database),
        transactions=SqlTransactionRunner(container.database),
    )(status_changed("paid", "preparing"))

    assert [
        r for r in await outbox_rows(container) if r["name"] == "payments.checkout.close_requested"
    ] == []


async def test_a_gateway_failure_leaves_the_stored_checkout_open(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)
    due = NOW + timedelta(hours=48)
    await in_transaction(container, lambda: repo.upsert(make_checkout(expires_at=due)))
    handler = CloseCheckout(
        gateway=FakeGateway(fail=True),
        checkouts=repo,
        transactions=SqlTransactionRunner(container.database),
        clock=SystemClock(),
    )

    with pytest.raises(RuntimeError, match="payments.gateway_unavailable"):
        await handler(
            EventMessage(
                id=UUID(int=6),
                name="payments.checkout.close_requested",
                payload={"order_id": str(ORDER)},
                occurred_at=NOW,
            )
        )

    stored = await in_transaction(container, lambda: repo.get_for_order(ORDER))
    assert stored is not None
    assert stored.expires_at == due
