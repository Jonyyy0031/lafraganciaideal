"""`SqlPaymentRepository` on PostgreSQL (`fragancia_test`): read back whole, the `ON CONFLICT`
upsert by payment id, the `FOR UPDATE` lock, joining the active transaction (a rollback stores
nothing), the stored columns and the column limits."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError

from fragancia_api.container import Container
from fragancia_api.modules.payments.domain.payment import Payment, PaymentOutcome
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import (
    SqlPaymentRepository,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import row, rows

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ORDER = UUID(int=900)


def make_payment(payment_id: str = "1234567890", **overrides: object) -> Payment:
    values: dict[str, object] = {
        "payment_id": payment_id,
        "order_id": ORDER,
        "status": "approved",
        "status_detail": "accredited",
        "amount_cents": 268_000,
        "currency": "MXN",
        "payment_type": "credit_card",
        "approved_at": T0 - timedelta(seconds=20),
        "outcome": None,
        "created_at": T0,
        "updated_at": T0,
    }
    values.update(overrides)
    return Payment(**values)  # type: ignore[arg-type]


async def put(container: Container, payment: Payment) -> None:
    repo = SqlPaymentRepository(container.database)
    await in_transaction(container, lambda: repo.save(payment))


async def get(container: Container, payment_id: str = "1234567890") -> Payment | None:
    """The stored payment, read through `claim` (which keeps a stored row as it is); None
    when the payment has no row (checked first, since `claim` would insert one)."""
    found = await row(
        container, "SELECT count(*) AS n FROM payments.payments WHERE payment_id = :p", p=payment_id
    )
    if int(found["n"]) == 0:
        return None
    repo = SqlPaymentRepository(container.database)
    probe = make_payment(payment_id, status="probe")
    return await in_transaction(container, lambda: repo.claim(probe))


async def count(container: Container) -> int:
    found = await row(container, "SELECT count(*) AS n FROM payments.payments")
    return int(found["n"])


async def test_an_unknown_payment_finds_none(container: Container) -> None:
    assert await get(container) is None


async def test_a_stored_payment_is_read_back_whole(container: Container) -> None:
    payment = make_payment()

    await put(container, payment)

    assert await get(container) == payment


async def test_a_payment_with_every_optional_field_empty_is_read_back_whole(
    container: Container,
) -> None:
    payment = make_payment(
        order_id=None, status="pending", status_detail=None, payment_type=None, approved_at=None
    )

    await put(container, payment)

    assert await get(container) == payment


@pytest.mark.parametrize("outcome", list(PaymentOutcome))
async def test_every_outcome_round_trips_as_its_enum(
    container: Container, outcome: PaymentOutcome
) -> None:
    await put(container, make_payment(outcome=outcome))

    stored = await get(container)

    assert stored is not None
    assert stored.outcome is outcome


async def test_the_columns_are_stored_as_given(container: Container) -> None:
    await put(container, make_payment(outcome=PaymentOutcome.REFUND_DUE))

    stored = await row(container, "SELECT * FROM payments.payments")

    assert stored == {
        "payment_id": "1234567890",
        "order_id": ORDER,
        "status": "approved",
        "status_detail": "accredited",
        "amount_cents": 268_000,
        "currency": "MXN",
        "payment_type": "credit_card",
        "approved_at": T0 - timedelta(seconds=20),
        "outcome": "refund_due",
        "created_at": T0,
        "updated_at": T0,
        # Migration 0025 (plan 003): empty for a card payment.
        "payment_method": None,
        "expires_at": None,
        "ticket_url": None,
        "held_at": None,
    }


async def test_the_instants_keep_their_moment_in_any_offset(container: Container) -> None:
    mexico = timezone(timedelta(hours=-6))
    approved = datetime(2026, 10, 24, 6, 0, tzinfo=mexico)

    await put(container, make_payment(approved_at=approved))

    stored = await get(container)
    assert stored is not None
    assert stored.approved_at == approved == datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


async def test_a_second_save_replaces_the_row_of_the_same_payment(container: Container) -> None:
    await put(container, make_payment(status="pending", approved_at=None, status_detail=None))
    later = T0 + timedelta(minutes=3)
    settled = make_payment(outcome=PaymentOutcome.APPLIED, updated_at=later)

    await put(container, settled)

    assert await count(container) == 1
    assert await get(container) == settled


async def test_a_second_save_never_changes_created_at(container: Container) -> None:
    await put(container, make_payment())

    await put(container, make_payment(created_at=T0 + timedelta(days=1), status="refunded"))

    stored = await get(container)
    assert stored is not None
    assert stored.created_at == T0
    assert stored.status == "refunded"


async def test_each_payment_has_its_own_row(container: Container) -> None:
    await put(container, make_payment("1", status="rejected"))
    await put(container, make_payment("2", status="approved"))

    first = await get(container, "1")
    second = await get(container, "2")

    assert await count(container) == 2
    assert first is not None
    assert second is not None
    assert (first.status, second.status) == ("rejected", "approved")


async def test_two_payments_may_share_an_order(container: Container) -> None:
    await put(container, make_payment("1"))
    await put(container, make_payment("2"))

    [found] = await rows(
        container, "SELECT count(*) AS n FROM payments.payments WHERE order_id = :o", o=ORDER
    )
    assert found["n"] == 2


async def test_a_rolled_back_save_stores_nothing(container: Container) -> None:
    repo = SqlPaymentRepository(container.database)

    async def work() -> Result[None, DomainError]:
        await repo.save(make_payment())
        return Err(DomainError(code="X", message="rolled back"))

    result = await SqlTransactionRunner(container.database).run(work)

    assert isinstance(result, Err)
    assert await count(container) == 0


async def test_a_committed_save_is_visible_to_the_next_transaction(container: Container) -> None:
    repo = SqlPaymentRepository(container.database)

    async def work() -> Result[None, DomainError]:
        await repo.save(make_payment())
        return Ok(None)

    assert isinstance(await SqlTransactionRunner(container.database).run(work), Ok)
    assert await count(container) == 1


async def test_claim_locks_the_row_until_the_transaction_ends(
    container: Container,
) -> None:
    await put(container, make_payment())
    repo = SqlPaymentRepository(container.database)
    first_holds = asyncio.Event()
    release_first = asyncio.Event()
    order_of_events: list[str] = []

    async def first() -> Result[None, DomainError]:
        found = await repo.claim(make_payment())
        assert found is not None
        first_holds.set()
        await asyncio.wait_for(release_first.wait(), 10)
        order_of_events.append("first commits")
        return Ok(None)

    async def second() -> Result[None, DomainError]:
        order_of_events.append("second asks")
        found = await repo.claim(make_payment())
        assert found is not None
        order_of_events.append("second got it")
        return Ok(None)

    runner = SqlTransactionRunner(container.database)
    task_one = asyncio.create_task(runner.run(first))
    await asyncio.wait_for(first_holds.wait(), 10)
    task_two = asyncio.create_task(runner.run(second))
    await asyncio.sleep(0.5)
    assert order_of_events == ["second asks"]  # blocked behind the first transaction
    release_first.set()
    await asyncio.wait_for(asyncio.gather(task_one, task_two), 10)

    assert order_of_events == ["second asks", "first commits", "second got it"]


async def test_claim_inserts_the_row_when_the_payment_is_unknown(container: Container) -> None:
    repo = SqlPaymentRepository(container.database)
    fresh = make_payment("777", status="pending", approved_at=None)

    claimed = await in_transaction(container, lambda: repo.claim(fresh))

    assert claimed == fresh
    assert await count(container) == 1
    stored = await row(container, "SELECT * FROM payments.payments WHERE payment_id = '777'")
    assert (stored["status"], stored["outcome"], stored["created_at"]) == ("pending", None, T0)


async def test_claim_keeps_the_stored_row_and_its_outcome(container: Container) -> None:
    stored = make_payment(outcome=PaymentOutcome.APPLIED, updated_at=T0 + timedelta(minutes=1))
    await put(container, stored)
    repo = SqlPaymentRepository(container.database)
    offered = make_payment(
        status="pending", amount_cents=1, outcome=None, created_at=T0 + timedelta(days=1)
    )

    claimed = await in_transaction(container, lambda: repo.claim(offered))

    assert claimed == stored
    assert await count(container) == 1
    assert await get(container) == stored


async def test_a_rolled_back_claim_leaves_no_row(container: Container) -> None:
    repo = SqlPaymentRepository(container.database)

    async def work() -> Result[None, DomainError]:
        await repo.claim(make_payment())
        return Err(DomainError(code="X", message="rolled back"))

    assert isinstance(await SqlTransactionRunner(container.database).run(work), Err)
    assert await count(container) == 0


async def test_a_claim_of_a_new_payment_blocks_a_second_claim_of_the_same_id(
    container: Container,
) -> None:
    # Plan 002 review H1: with no row yet, `SELECT … FOR UPDATE` alone locks nothing; the
    # insert-then-lock makes the second delivery wait and then see what the first one stored.
    repo = SqlPaymentRepository(container.database)
    first_holds = asyncio.Event()
    release_first = asyncio.Event()
    seen: list[str] = []
    second_saw: list[PaymentOutcome | None] = []

    async def first() -> Result[None, DomainError]:
        claimed = await repo.claim(make_payment())
        claimed.settle(PaymentOutcome.APPLIED, T0)
        await repo.save(claimed)
        first_holds.set()
        await asyncio.wait_for(release_first.wait(), 10)
        seen.append("first commits")
        return Ok(None)

    async def second() -> Result[None, DomainError]:
        seen.append("second asks")
        claimed = await repo.claim(make_payment())
        second_saw.append(claimed.outcome)
        seen.append("second got it")
        return Ok(None)

    runner = SqlTransactionRunner(container.database)
    task_one = asyncio.create_task(runner.run(first))
    await asyncio.wait_for(first_holds.wait(), 10)
    task_two = asyncio.create_task(runner.run(second))
    await asyncio.sleep(0.5)
    assert seen == ["second asks"]
    release_first.set()
    await asyncio.wait_for(asyncio.gather(task_one, task_two), 10)

    assert seen == ["second asks", "first commits", "second got it"]
    assert second_saw == [PaymentOutcome.APPLIED]
    assert await count(container) == 1


async def test_values_over_their_limits_are_refused(container: Container) -> None:
    with pytest.raises(DBAPIError):
        await put(container, make_payment("9" * 33))
    with pytest.raises(DBAPIError):
        await put(container, make_payment(currency="MXNN"))
    await put(container, make_payment("9" * 32))

    assert await count(container) == 1
