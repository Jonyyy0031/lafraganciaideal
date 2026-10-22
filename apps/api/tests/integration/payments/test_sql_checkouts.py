"""`SqlCheckoutRepository` on PostgreSQL (`fragancia_test`): read, the `ON CONFLICT` upsert, one
row per order, joining the active transaction (a rollback stores nothing) and the column limits."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from fragancia_api.container import Container
from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.infrastructure.sql_checkout_repository import (
    SqlCheckoutRepository,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.orders.test_sql_carts import in_transaction
from tests.integration.orders.test_sql_orders import row, rows

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ORDER = UUID(int=900)


def make_checkout(
    order_id: UUID = ORDER, *, preference_id: str = "pref-1", expires_at: datetime | None = None
) -> Checkout:
    return Checkout(
        order_id=order_id,
        preference_id=preference_id,
        redirect_url=f"https://pay.test/{preference_id}",
        expires_at=expires_at or T0 + timedelta(minutes=30),
        created_at=T0,
    )


async def put(container: Container, checkout: Checkout) -> None:
    repo = SqlCheckoutRepository(container.database)
    await in_transaction(container, lambda: repo.upsert(checkout))


async def get(container: Container, order_id: UUID = ORDER) -> Checkout | None:
    repo = SqlCheckoutRepository(container.database)
    return await in_transaction(container, lambda: repo.get_for_order(order_id))


async def count(container: Container) -> int:
    found = await row(container, "SELECT count(*) AS n FROM payments.checkouts")
    return int(found["n"])


async def test_an_order_without_a_checkout_finds_none(container: Container) -> None:
    assert await get(container) is None


async def test_a_stored_checkout_is_read_back_whole(container: Container) -> None:
    checkout = make_checkout()

    await put(container, checkout)

    assert await get(container) == checkout


async def test_the_columns_are_stored_as_given(container: Container) -> None:
    await put(container, make_checkout(preference_id="123-abc"))

    stored = await row(container, "SELECT * FROM payments.checkouts")

    assert stored == {
        "order_id": ORDER,
        "preference_id": "123-abc",
        "redirect_url": "https://pay.test/123-abc",
        "expires_at": T0 + timedelta(minutes=30),
        "created_at": T0,
    }


async def test_the_instants_keep_their_moment_in_any_offset(container: Container) -> None:
    mexico = timezone(timedelta(hours=-6))
    checkout = make_checkout(expires_at=datetime(2026, 10, 22, 12, 30, tzinfo=mexico))

    await put(container, checkout)

    stored = await get(container)
    assert stored is not None
    assert stored.expires_at == checkout.expires_at
    assert stored.expires_at == datetime(2026, 10, 22, 18, 30, tzinfo=UTC)


async def test_a_second_upsert_replaces_the_row_of_the_same_order(container: Container) -> None:
    await put(container, make_checkout(preference_id="old"))
    newer = Checkout(
        order_id=ORDER,
        preference_id="new",
        redirect_url="https://pay.test/new",
        expires_at=T0 + timedelta(hours=1),
        created_at=T0 + timedelta(minutes=5),
    )

    await put(container, newer)

    assert await count(container) == 1
    assert await get(container) == newer


async def test_each_order_has_its_own_row(container: Container) -> None:
    other = UUID(int=901)
    await put(container, make_checkout(ORDER, preference_id="a"))
    await put(container, make_checkout(other, preference_id="b"))

    assert await count(container) == 2
    first = await get(container, ORDER)
    second = await get(container, other)
    assert first is not None
    assert second is not None
    assert (first.preference_id, second.preference_id) == ("a", "b")


async def test_the_order_id_is_the_primary_key_and_not_a_foreign_key(container: Container) -> None:
    # No order UUID(int=900) exists: payments does not reference orders' schema.
    await put(container, make_checkout())

    keys = await rows(
        container,
        "SELECT contype FROM pg_constraint WHERE conrelid = 'payments.checkouts'::regclass "
        "AND contype <> 'n'",  # PostgreSQL 18 lists NOT NULL as 'n' constraints
    )
    assert [k["contype"] for k in keys] == ["p"]


async def test_a_rolled_back_upsert_stores_nothing(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)

    async def work() -> Result[None, DomainError]:
        await repo.upsert(make_checkout())
        return Err(DomainError(code="X", message="rolled back"))

    result = await SqlTransactionRunner(container.database).run(work)

    assert isinstance(result, Err)
    assert await count(container) == 0


async def test_a_committed_upsert_is_visible_to_the_next_transaction(container: Container) -> None:
    repo = SqlCheckoutRepository(container.database)

    async def work() -> Result[None, DomainError]:
        await repo.upsert(make_checkout())
        return Ok(None)

    assert isinstance(await SqlTransactionRunner(container.database).run(work), Ok)
    assert await count(container) == 1


async def test_a_preference_id_over_64_characters_is_refused(container: Container) -> None:
    with pytest.raises(DBAPIError):
        await put(container, make_checkout(preference_id="p" * 65))
    await put(container, make_checkout(preference_id="p" * 64))

    assert await count(container) == 1


async def test_a_redirect_url_over_500_characters_is_refused(container: Container) -> None:
    long = Checkout(
        order_id=new_id(),
        preference_id="p",
        redirect_url="https://pay.test/" + "u" * 484,
        expires_at=T0,
        created_at=T0,
    )
    assert len(long.redirect_url) == 501

    with pytest.raises(DBAPIError):
        await put(container, long)
    ok = Checkout(long.order_id, "p", long.redirect_url[:500], T0, T0)
    await put(container, ok)

    assert await count(container) == 1


async def test_the_table_lives_in_the_payments_schema(container: Container) -> None:
    async with container.database.engine.connect() as connection:
        found = (
            (
                await connection.execute(
                    text(
                        "SELECT table_schema FROM information_schema.tables "
                        "WHERE table_name = 'checkouts'"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert found == ["payments"]
