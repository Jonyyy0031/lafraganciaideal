"""Carts on PostgreSQL (`fragancia_test`): the repository, the reader, the table constraints,
the FK cascade, the row lock, the purge, and the use cases over the real adapters and the real
catalog facade."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError

from fragancia_api.container import MODULES, Container
from fragancia_api.modules.catalog.infrastructure.tables import presentations
from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    PurgeCarts,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME, Cart, CartLine
from fragancia_api.modules.orders.infrastructure.sql_cart_repository import (
    SqlCartReader,
    SqlCartRepository,
)
from fragancia_api.modules.orders.infrastructure.tables import cart_lines, carts
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration

TIMEOUT = 5
WAIT = 0.5
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


async def in_transaction[T](container: Container, work: Callable[[], Awaitable[T]]) -> T:
    async def unit() -> Result[T, DomainError]:
        return Ok(await work())

    result = await SqlTransactionRunner(container.database).run(unit)
    assert isinstance(result, Ok)
    value: T = result.value
    return value


def make_cart(digest: str, *lines: CartLine, updated_at: datetime = T0) -> Cart:
    return Cart(
        id=new_id(),
        token_digest=digest,
        lines=list(lines),
        created_at=T0 - timedelta(days=1),
        updated_at=updated_at,
    )


def line(quantity: int = 2, seen: int = 250_000, minutes: int = 0) -> CartLine:
    return CartLine(new_id(), quantity, seen, T0 + timedelta(minutes=minutes))


async def store(container: Container, cart: Cart) -> None:
    repo = SqlCartRepository(container.database)
    await in_transaction(container, lambda: repo.add(cart))


async def count(container: Container, table: str) -> int:
    # `table` is always a literal from this file.
    query = text(f"SELECT count(*) FROM orders.{table}")  # noqa: S608
    async with container.database.engine.connect() as connection:
        return int((await connection.execute(query)).scalar_one())


async def backdate(container: Container, cart_id: UUID, moment: datetime) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(
            update(carts).where(carts.c.id == cart_id).values(updated_at=moment)
        )


async def wait_until_a_delete_waits_on_a_lock(container: Container) -> None:
    """Poll `pg_stat_activity` until a `DELETE` on the carts is blocked on a lock, so a test
    never releases the lock holder before the waiter is actually waiting."""
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND wait_event_type = 'Lock' AND query ILIKE 'DELETE FROM orders.carts%'"
    )
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        async with container.database.engine.connect() as connection:
            if (await connection.execute(query)).scalar_one() >= 1:
                return
        await asyncio.sleep(0.05)
    pytest.fail("no DELETE on orders.carts is waiting on a lock")


# --- repository ---------------------------------------------------------------------------------


async def test_a_stored_cart_reads_back_with_its_lines_ordered_by_when_they_were_added(
    container: Container,
) -> None:
    later, earlier = line(minutes=5), line(3, 100, minutes=1)
    cart = make_cart("a" * 64, later, earlier)
    repo = SqlCartRepository(container.database)
    await store(container, cart)

    found = await in_transaction(container, lambda: repo.get_by_token_for_update("a" * 64))

    assert found is not None
    assert (found.id, found.token_digest) == (cart.id, "a" * 64)
    assert (found.created_at, found.updated_at) == (cart.created_at, cart.updated_at)
    assert found.lines == [earlier, later]


async def test_an_unknown_digest_finds_nothing(container: Container) -> None:
    repo = SqlCartRepository(container.database)

    assert await in_transaction(container, lambda: repo.get_by_token_for_update("f" * 64)) is None
    assert await SqlCartReader(container.database).by_token("f" * 64) is None


async def test_the_reader_returns_the_same_cart_outside_a_transaction(
    container: Container,
) -> None:
    cart = make_cart("b" * 64, line(), line(minutes=2))
    await store(container, cart)

    found = await SqlCartReader(container.database).by_token("b" * 64)

    assert found is not None
    assert (found.id, found.lines) == (cart.id, cart.lines)


async def test_save_replaces_the_lines_and_the_updated_at(container: Container) -> None:
    kept, dropped = line(minutes=1), line(minutes=2)
    cart = make_cart("c" * 64, kept, dropped)
    await store(container, cart)
    repo = SqlCartRepository(container.database)

    async def change() -> None:
        loaded = await repo.get_by_token_for_update("c" * 64)
        assert loaded is not None
        loaded.remove_line(dropped.presentation_id, T0 + timedelta(hours=1))
        loaded.set_line(kept.presentation_id, 9, 111_000, T0 + timedelta(hours=1))
        loaded.set_line(new_id(), 1, 5, T0 + timedelta(hours=1))
        await repo.save(loaded)

    await in_transaction(container, change)

    found = await SqlCartReader(container.database).by_token("c" * 64)
    assert found is not None
    assert found.updated_at == T0 + timedelta(hours=1)
    assert [(x.quantity, x.seen_price_cents) for x in found.lines] == [(9, 111_000), (1, 5)]
    assert found.lines[0].presentation_id == kept.presentation_id
    assert await count(container, "carts") == 1


async def test_saving_a_cart_without_lines_removes_them_all(container: Container) -> None:
    cart = make_cart("d" * 64, line(), line(minutes=1))
    await store(container, cart)
    repo = SqlCartRepository(container.database)

    async def empty() -> None:
        loaded = await repo.get_by_token_for_update("d" * 64)
        assert loaded is not None
        loaded.lines = []
        await repo.save(loaded)

    await in_transaction(container, empty)

    assert await count(container, "cart_lines") == 0
    assert await count(container, "carts") == 1


async def test_a_rolled_back_transaction_keeps_nothing(container: Container) -> None:
    repo = SqlCartRepository(container.database)
    cart = make_cart("e" * 64, line())

    async def failing() -> Result[None, DomainError]:
        await repo.add(cart)
        return Err(DomainError(code="X", message="rolled back"))

    result = await SqlTransactionRunner(container.database).run(failing)

    assert isinstance(result, Err)
    assert await count(container, "carts") == 0
    assert await count(container, "cart_lines") == 0


# --- constraints --------------------------------------------------------------------------------


async def test_the_token_digest_is_unique(container: Container) -> None:
    await store(container, make_cart("u" * 64))

    with pytest.raises(IntegrityError):
        await store(container, make_cart("u" * 64))


@pytest.mark.parametrize("quantity", [0, -1, 11])
async def test_the_database_rejects_a_line_quantity_outside_1_to_10(
    container: Container, quantity: int
) -> None:
    cart = make_cart("q" * 64)
    await store(container, cart)

    with pytest.raises(IntegrityError):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(cart_lines).values(
                    cart_id=cart.id,
                    presentation_id=new_id(),
                    quantity=quantity,
                    seen_price_cents=1,
                    added_at=T0,
                )
            )


@pytest.mark.parametrize("quantity", [1, 10])
async def test_the_database_accepts_the_quantity_bounds(
    container: Container, quantity: int
) -> None:
    cart = make_cart("q" * 64, line(quantity))

    await store(container, cart)

    assert await count(container, "cart_lines") == 1


async def test_a_presentation_appears_once_per_cart(container: Container) -> None:
    cart = make_cart("p" * 64, line())
    await store(container, cart)

    with pytest.raises(IntegrityError):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(cart_lines).values(
                    cart_id=cart.id,
                    presentation_id=cart.lines[0].presentation_id,
                    quantity=1,
                    seen_price_cents=1,
                    added_at=T0,
                )
            )


async def test_a_line_needs_an_existing_cart(container: Container) -> None:
    with pytest.raises(IntegrityError):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(cart_lines).values(
                    cart_id=new_id(),
                    presentation_id=new_id(),
                    quantity=1,
                    seen_price_cents=1,
                    added_at=T0,
                )
            )


async def test_deleting_a_cart_cascades_to_its_lines_only(container: Container) -> None:
    doomed = make_cart("1" * 64, line(), line(minutes=1))
    other = make_cart("2" * 64, line())
    await store(container, doomed)
    await store(container, other)

    async with container.database.engine.begin() as connection:
        await connection.execute(carts.delete().where(carts.c.id == doomed.id))

    assert await count(container, "carts") == 1
    assert await count(container, "cart_lines") == 1


# --- purge --------------------------------------------------------------------------------------


async def test_delete_untouched_before_removes_only_older_carts_with_their_lines(
    container: Container,
) -> None:
    cutoff = T0
    old = make_cart("o" * 64, line(), line(minutes=1), updated_at=cutoff - timedelta(seconds=1))
    edge = make_cart("e" * 64, line(), updated_at=cutoff)
    new = make_cart("n" * 64, line(), updated_at=cutoff + timedelta(days=1))
    for cart in (old, edge, new):
        await store(container, cart)
    repo = SqlCartRepository(container.database)

    deleted = await in_transaction(container, lambda: repo.delete_untouched_before(cutoff, 500))

    assert deleted == 1
    assert await SqlCartReader(container.database).by_token("o" * 64) is None
    assert await SqlCartReader(container.database).by_token("e" * 64) is not None
    assert await SqlCartReader(container.database).by_token("n" * 64) is not None
    assert await count(container, "cart_lines") == 2


async def test_delete_untouched_before_takes_the_oldest_first_up_to_the_limit(
    container: Container,
) -> None:
    for index in range(5):
        await store(
            container,
            make_cart(f"{index}" * 64, updated_at=T0 - timedelta(days=10 - index)),
        )
    repo = SqlCartRepository(container.database)

    deleted = await in_transaction(container, lambda: repo.delete_untouched_before(T0, 3))

    assert deleted == 3
    reader = SqlCartReader(container.database)
    assert [await reader.by_token(f"{i}" * 64) is None for i in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]


async def test_delete_untouched_before_with_nothing_old_deletes_nothing(
    container: Container,
) -> None:
    await store(container, make_cart("k" * 64))
    repo = SqlCartRepository(container.database)

    assert (
        await in_transaction(
            container, lambda: repo.delete_untouched_before(T0 - CART_LIFETIME, 500)
        )
        == 0
    )
    assert await count(container, "carts") == 1


# --- the row lock -------------------------------------------------------------------------------


async def test_get_by_token_for_update_blocks_a_second_writer_until_the_first_commits(
    container: Container,
) -> None:
    await store(container, make_cart("l" * 64, line(1)))
    repo = SqlCartRepository(container.database)
    runner = SqlTransactionRunner(container.database)
    holding = asyncio.Event()
    release = asyncio.Event()
    second_done = asyncio.Event()

    async def first() -> Result[None, DomainError]:
        cart = await repo.get_by_token_for_update("l" * 64)
        assert cart is not None
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)
        cart.set_line(new_id(), 1, 100, T0)
        await repo.save(cart)
        return Ok(None)

    async def second() -> Result[None, DomainError]:
        cart = await repo.get_by_token_for_update("l" * 64)
        assert cart is not None
        # It only gets the row after the first committed, so it sees the first one's line.
        assert len(cart.lines) == 2
        cart.set_line(new_id(), 1, 100, T0)
        await repo.save(cart)
        second_done.set()
        return Ok(None)

    first_task = asyncio.create_task(runner.run(first))
    second_task: asyncio.Task[Result[None, DomainError]] | None = None
    try:
        await asyncio.wait_for(holding.wait(), TIMEOUT)
        second_task = asyncio.create_task(runner.run(second))
        await asyncio.sleep(WAIT)
        assert not second_done.is_set(), "the second writer got the cart while it was locked"
        release.set()
        assert isinstance(await asyncio.wait_for(first_task, TIMEOUT), Ok)
        assert isinstance(await asyncio.wait_for(second_task, TIMEOUT), Ok)
    finally:
        release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()

    final = await SqlCartReader(container.database).by_token("l" * 64)
    assert final is not None
    assert len(final.lines) == 3


# --- use cases over the real adapters and the real catalog facade -------------------------------


async def test_the_whole_cart_flow_over_the_real_adapters(
    container: Container, catalog_rows: CatalogRows
) -> None:
    services = container.services
    _, [in_stock, made_to_order] = await catalog_rows.perfume(
        "Flow",
        sizes=(
            {"stock_available": 3},
            {
                "availability": "made_to_order",
                "lead_time_min_days": 3,
                "lead_time_max_days": 6,
                "price_cents": 100_000,
            },
        ),
    )

    created = await services.get(SetCartLine).execute(None, in_stock, 2)
    assert isinstance(created, Ok)
    token = created.value.token
    assert token is not None
    assert created.value.view.subtotal_cents == 500_000
    async with container.database.engine.connect() as connection:
        digests = (
            (await connection.execute(text("SELECT token_digest FROM orders.carts")))
            .scalars()
            .all()
        )
    assert len(digests) == 1
    assert token not in digests
    assert len(digests[0]) == 64

    more = await services.get(SetCartLine).execute(token, made_to_order, 10)
    assert isinstance(more, Ok)
    assert more.value.token == token
    view = await services.get(GetCart).execute(token)
    assert [line.status for line in view.lines] == ["ok", "ok"]
    assert (view.subtotal_cents, view.item_count, view.lead_time_max_days) == (1_500_000, 12, 6)

    over = await services.get(SetCartLine).execute(token, in_stock, 4)
    assert isinstance(over, Err)
    assert over.error.code == "ORDERS_CART_STOCK_UNAVAILABLE"
    assert over.error.details == {"available": 3}

    removed = await services.get(RemoveCartLine).execute(token, made_to_order)
    assert isinstance(removed, Ok)
    assert [line.presentation_id for line in removed.value.view.lines] == [in_stock]
    assert await count(container, "cart_lines") == 1


async def test_a_price_change_shows_until_the_buyer_acknowledges_it(
    container: Container, catalog_rows: CatalogRows
) -> None:
    services = container.services
    _, [presentation_id] = await catalog_rows.perfume("Price", sizes=({"stock_available": 5},))
    created = await services.get(SetCartLine).execute(None, presentation_id, 1)
    assert isinstance(created, Ok)
    token = created.value.token
    async with container.database.engine.begin() as connection:
        await connection.execute(
            update(presentations)
            .where(presentations.c.id == presentation_id)
            .values(price_cents=280_000)
        )

    changed = (await services.get(GetCart).execute(token)).lines[0]
    acknowledged = await services.get(AcknowledgeCartPrices).execute(token)
    after = (await services.get(GetCart).execute(token)).lines[0]

    assert (changed.unit_price_cents, changed.price_changed_from_cents) == (280_000, 250_000)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.view.lines[0].price_changed_from_cents is None
    assert after.price_changed_from_cents is None


async def test_a_line_that_sells_out_is_flagged_and_comes_back_when_stock_returns(
    container: Container, catalog_rows: CatalogRows
) -> None:
    services = container.services
    _, [presentation_id] = await catalog_rows.perfume("Stock", sizes=({"stock_available": 5},))
    created = await services.get(SetCartLine).execute(None, presentation_id, 2)
    assert isinstance(created, Ok)
    token = created.value.token

    async def set_stock(units: int) -> None:
        async with container.database.engine.begin() as connection:
            await connection.execute(
                update(presentations)
                .where(presentations.c.id == presentation_id)
                .values(stock_available=units)
            )

    await set_stock(0)
    sold_out = await services.get(GetCart).execute(token)
    await set_stock(5)
    back = await services.get(GetCart).execute(token)

    assert (sold_out.lines[0].status, sold_out.subtotal_cents, sold_out.has_problems) == (
        "sold_out",
        0,
        True,
    )
    assert (back.lines[0].status, back.subtotal_cents, back.has_problems) == ("ok", 500_000, False)


async def test_getting_an_unknown_token_writes_nothing(container: Container) -> None:
    view = await container.services.get(GetCart).execute("never-issued")

    assert view.lines == []
    assert await count(container, "carts") == 0


async def test_the_purge_job_deletes_a_cart_31_days_old_with_its_lines_and_keeps_a_recent_one(
    container: Container,
) -> None:
    stale = make_cart("s" * 64, line(), line(minutes=1))
    fresh = make_cart("f" * 64, line())
    await store(container, stale)
    await store(container, fresh)
    now = datetime.now(UTC)
    await backdate(container, stale.id, now - timedelta(days=31))
    await backdate(container, fresh.id, now - timedelta(days=29))

    deleted = await container.services.get(PurgeCarts).run_once()

    assert deleted == 1
    reader = SqlCartReader(container.database)
    assert await reader.by_token("s" * 64) is None
    assert await reader.by_token("f" * 64) is not None
    assert await count(container, "cart_lines") == 1


async def test_the_scheduled_orders_job_runs_the_purge_against_the_database(
    container: Container,
) -> None:
    stale = make_cart("s" * 64, line())
    await store(container, stale)
    await backdate(container, stale.id, datetime.now(UTC) - timedelta(days=40))
    orders = next(module for module in MODULES if module.name == "orders")
    job = next(job for job in orders.jobs if job.name == "orders_purge_carts")

    assert await job.run(container.services) == 1
    assert await count(container, "carts") == 0


async def test_a_purge_waiting_on_a_cart_that_is_touched_meanwhile_keeps_it(
    container: Container,
) -> None:
    # The purge picks the stale cart, then waits on the row a buyer's write holds; once that
    # write commits, PostgreSQL re-checks the outer WHERE only, so it must repeat the age test.
    digest = "r" * 64
    cutoff = T0
    await store(container, make_cart(digest, line(1), updated_at=cutoff - timedelta(days=1)))
    repo = SqlCartRepository(container.database)
    runner = SqlTransactionRunner(container.database)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def touch() -> Result[None, DomainError]:
        cart = await repo.get_by_token_for_update(digest)
        assert cart is not None
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)
        cart.set_line(new_id(), 1, 100, cutoff + timedelta(days=1))
        await repo.save(cart)
        return Ok(None)

    async def purge() -> Result[int, DomainError]:
        return Ok(await repo.delete_untouched_before(cutoff, 500))

    touch_task = asyncio.create_task(runner.run(touch))
    purge_task: asyncio.Task[Result[int, DomainError]] | None = None
    try:
        await asyncio.wait_for(holding.wait(), TIMEOUT)
        purge_task = asyncio.create_task(runner.run(purge))
        await wait_until_a_delete_waits_on_a_lock(container)
        assert not purge_task.done(), "the purge did not wait for the row lock"
        release.set()
        assert isinstance(await asyncio.wait_for(touch_task, TIMEOUT), Ok)
        purged = await asyncio.wait_for(purge_task, TIMEOUT)
    finally:
        release.set()
        for task in (touch_task, purge_task):
            if task is not None and not task.done():
                task.cancel()

    assert purged == Ok(0)
    survivor = await SqlCartReader(container.database).by_token(digest)
    assert survivor is not None
    assert len(survivor.lines) == 2
