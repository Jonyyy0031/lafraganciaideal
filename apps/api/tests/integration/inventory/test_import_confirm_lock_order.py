"""Plan 003, repair round 2 (re-review M2) on PostgreSQL: the import confirm and a multi-perfume
reserve take presentation rows in the same global order, so they cannot deadlock.

1. `SqlPerfumeRepository.lock_presentations` locks every stored presentation of the given
   perfumes, ascending by id, and does nothing for an empty list.
2. A reserve of two perfumes and an import confirm updating both, with the identity order
   (name slug) opposite to the presentation id order, both finish. Against the round-1 confirm
   (no up-front locks) it deadlocks: the confirm wrote the higher-id row first, then waited on
   the lower-id row the reserve share-locks while the reserve waited on the higher one.

Both are deterministic: one side is paused holding locks while the other starts. Gates open and
tasks finish or get cancelled when the `Scenario` block ends, and every wait is bounded by
`TIMEOUT`, so a regression fails within seconds and never hangs the suite."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.catalog.infrastructure.sql_perfume_repository import (
    SqlPerfumeRepository,
)
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.import_fixtures import (
    World,
    seed_perfume,
    world,  # noqa: F401  (a fixture)
)
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.catalog.test_sql_catalog_import_confirm import (
    _confirm_with,
    _planned,
    _presentation,
    _presentation_id,
    _store_ready,
)
from tests.integration.inventory.reservation_support import Commands, in_minutes, item_row
from tests.integration.inventory.test_reservation_lock_order import PausingCatalog
from tests.integration.inventory.test_stock_settings_race import (
    TIMEOUT,
    Scenario,
    is_blocked,
    plain,
    run_in_transaction,
)
from tests.integration.inventory.test_stock_status_flow import restock

pytestmark = pytest.mark.integration


async def is_locked(container: Container, presentation_id: UUID) -> bool:
    """True when another transaction holds the presentation row (SKIP LOCKED skips it)."""
    async with container.database.engine.begin() as connection:
        rows = (
            await connection.execute(
                text("SELECT id FROM catalog.presentations WHERE id = :p FOR UPDATE SKIP LOCKED"),
                {"p": presentation_id},
            )
        ).all()
    return not rows


async def becomes_locked(container: Container, presentation_id: UUID) -> bool:
    """Polls (bounded by `TIMEOUT`) until the row is held by someone else."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while loop.time() < deadline:
        if await is_locked(container, presentation_id):
            return True
        await asyncio.sleep(0.05)
    return False


async def lock_row(container: Container, presentation_id: UUID) -> None:
    await container.database.session.execute(
        text("SELECT id FROM catalog.presentations WHERE id = :p FOR UPDATE"),
        {"p": presentation_id},
    )


# --- lock_presentations ----------------------------------------------------------------------


async def test_lock_presentations_locks_every_presentation_of_the_given_perfumes_only(
    container: Container, catalog_rows: CatalogRows
) -> None:
    first, first_ids = await catalog_rows.perfume("First", sizes=({"ml": 50}, {"ml": 100}))
    second, second_ids = await catalog_rows.perfume("Second", sizes=({"ml": 50},))
    _, other_ids = await catalog_rows.perfume("Other", sizes=({"ml": 50},))
    perfumes = SqlPerfumeRepository(container.database)

    async def lock() -> None:
        await perfumes.lock_presentations([first, second])

    async with Scenario(container) as scenario:
        await scenario.hold(lock)

        for presentation_id in [*first_ids, *second_ids]:
            assert await is_locked(container, presentation_id)
        assert not await is_locked(container, other_ids[0])


async def test_lock_presentations_takes_the_rows_in_ascending_id_order(
    container: Container, catalog_rows: CatalogRows
) -> None:
    first, first_ids = await catalog_rows.perfume("First", sizes=({"ml": 50}, {"ml": 100}))
    second, second_ids = await catalog_rows.perfume("Second", sizes=({"ml": 50},))
    lowest, *_, highest = sorted([*first_ids, *second_ids])
    perfumes = SqlPerfumeRepository(container.database)

    async def lock_highest() -> None:
        await lock_row(container, highest)

    async def lock_all() -> None:
        await perfumes.lock_presentations([second, first])  # asked in the opposite order

    async with Scenario(container) as scenario:
        _, release = await scenario.hold(lock_highest)
        locking = scenario.spawn(run_in_transaction(container, lock_all))

        # It holds the lowest row while it waits on the highest one: ascending id order.
        assert await becomes_locked(container, lowest)
        assert await is_blocked(locking)
        release.set()
        await scenario.finish(locking)


async def test_lock_presentations_with_no_perfume_ids_locks_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, ids = await catalog_rows.perfume(sizes=({"ml": 50},))
    perfumes = SqlPerfumeRepository(container.database)

    async def lock_none() -> None:
        await perfumes.lock_presentations([])

    async with Scenario(container) as scenario:
        await scenario.hold(lock_none)

        assert not await is_locked(container, ids[0])


# --- the race ---------------------------------------------------------------------------------


async def test_a_two_perfume_reserve_and_an_import_confirm_updating_both_do_not_deadlock(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    """ "Zz Imp B" is stored first, so its presentation has the LOWER id, while the confirm applies
    perfumes by name slug ("zz-imp-a" first). The reserve share-locks the lower id first and is
    paused there; the confirm then starts. Without the up-front locks the confirm writes A's
    (higher) row, then waits on B's (lower) row, and the resumed reserve waits on A's row: a
    wait cycle that PostgreSQL breaks by aborting one of them, which `finish` reports."""
    perfume_b = await seed_perfume(container, world, "Zz Imp B", presentation_ml=(100,))
    perfume_a = await seed_perfume(container, world, "Zz Imp A", presentation_ml=(100,))
    low = await _presentation_id(container, perfume_b, 100)
    high = await _presentation_id(container, perfume_a, 100)
    assert low < high  # presentation id order is opposite to the identity (name slug) order
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.perfumes SET is_published = true WHERE id IN (:a, :b)"),
            {"a": perfume_a, "b": perfume_b},
        )
    await restock(container, low, 5)
    await restock(container, high, 5)
    import_id = await _store_ready(
        container,
        _planned(
            world,
            perfume_a,
            "Zz Imp A",
            is_new=False,
            first_row=2,
            presentations_=(_presentation(100, 111_100, presentation_id=high, row=2),),
            publish=True,
        ),
        _planned(
            world,
            perfume_b,
            "Zz Imp B",
            is_new=False,
            first_row=3,
            presentations_=(_presentation(100, 222_200, presentation_id=low, row=3),),
            publish=True,
        ),
    )
    confirm = _confirm_with(container, SqlTransactionRunner(container.database))

    async with Scenario(container) as scenario:
        release = scenario.gate()
        catalog = PausingCatalog(
            CatalogPresentations(
                PresentationDirectory(SqlPresentationDirectory(container.database))
            ),
            release,
        )
        reserve = scenario.spawn(
            Commands(container, plain(container), catalog=catalog).reserve.execute(
                "R-1", {high: 1, low: 1}, in_minutes(5)
            )
        )
        await scenario.reached(catalog.first_locked)  # the reserve share-locks the lower row
        confirming = scenario.spawn(confirm.execute(import_id, "admin-it", may_record_stock=True))

        assert await is_blocked(confirming)  # it needs the lower row the reserve holds
        release.set()
        reserve_result, confirm_result = await scenario.finish(reserve, confirming)  # no deadlock

    assert isinstance(reserve_result, Ok)
    assert isinstance(confirm_result, Ok)
    assert await item_row(container, low) == (5, 1)
    assert await item_row(container, high) == (5, 1)
    async with container.database.engine.connect() as connection:
        rows = await connection.execute(text("SELECT id, price_cents FROM catalog.presentations"))
        prices = {row.id: row.price_cents for row in rows}
    assert prices == {high: 111_100, low: 222_200}
