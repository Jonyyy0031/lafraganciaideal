"""Plan 003, repair round 1 (review L1) on PostgreSQL: the lock order the repair relies on.

1. A two-line reserve locks the lower `presentation_id` stock item before it waits on the higher
   one (the order every writer of several rows must share).
2. A two-size reserve and the catalog's perfume save of those sizes, created in reverse `ml`
   order, both finish: the save writes the presentations in `presentation_id` order, so it cannot
   wait on the reserve while the reserve waits on it.

Both are deterministic: one side is paused holding locks while the other starts. Gates open and
tasks finish or get cancelled when the `Scenario` block ends, and every wait is bounded by
`TIMEOUT`, so a lock-order regression fails within seconds and never hangs the suite."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
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
from fragancia_api.modules.inventory.application.ports import StockedPresentation
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import Commands, in_minutes, item_row
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
    """True when another transaction holds the stock item row (SKIP LOCKED skips it)."""
    async with container.database.engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT presentation_id FROM inventory.stock_items "
                    "WHERE presentation_id = :p FOR UPDATE SKIP LOCKED"
                ),
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


async def test_a_two_line_reserve_locks_the_lower_id_before_waiting_on_the_higher(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, ids = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 100}))
    low, high = sorted(ids)
    await restock(container, low, 5)
    await restock(container, high, 5)
    stock = SqlStockRepository(container.database)

    async def lock_high() -> None:
        await stock.get_many_for_update([high], now=datetime.now(UTC))

    async with Scenario(container) as scenario:
        _, release = await scenario.hold(lock_high)
        assert await is_locked(container, high)
        assert not await is_locked(container, low)

        # Asked high first: the reserve must still go through `low` first.
        reserve = scenario.spawn(
            Commands(container, plain(container)).reserve.execute(
                "R-1", {high: 1, low: 1}, in_minutes(5)
            )
        )

        assert await becomes_locked(container, low)  # held by the reserve, which now waits
        assert await is_blocked(reserve)  # on `high`, held by the paused transaction
        release.set()
        [result] = await scenario.finish(reserve)

    assert isinstance(result, Ok)
    assert await item_row(container, low) == (5, 1)
    assert await item_row(container, high) == (5, 1)


class PausingCatalog:
    """The real catalog, but the transaction pauses after the first presentation it share-locks
    (the second lock is not taken until `release` opens)."""

    def __init__(self, inner: CatalogPresentations, release: asyncio.Event) -> None:
        self._inner = inner
        self.release = release
        self.first_locked = asyncio.Event()
        self._calls = 0

    async def describe(self, ids: Sequence[UUID]) -> dict[UUID, StockedPresentation]:
        return await self._inner.describe(ids)

    async def describe_for_update(self, presentation_id: UUID) -> StockedPresentation | None:
        found = await self._inner.describe_for_update(presentation_id)
        self._calls += 1
        if self._calls == 1:
            self.first_locked.set()
            await asyncio.wait_for(self.release.wait(), timeout=2 * TIMEOUT)
        return found

    async def in_stock(self) -> list[StockedPresentation]:
        return await self._inner.in_stock()


async def test_a_two_size_reserve_and_a_perfume_save_of_those_sizes_do_not_deadlock(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """The presentations are created in reverse `ml` order: the lower id has the larger `ml`, so
    the aggregate (loaded by `ml`) lists the higher id first. A save that wrote them in that
    order would take the higher row, then wait on the reserve's lock of the lower one, while the
    reserve waits on the higher one: a wait cycle. Saving in id order, the save waits on the lower
    row holding nothing, and both finish."""
    perfume_id, ids = await catalog_rows.perfume(sizes=({"ml": 100}, {"ml": 50}))
    low, high = sorted(ids)
    async with container.database.engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT id, ml FROM catalog.presentations WHERE perfume_id = :p"),
            {"p": perfume_id},
        )
        ml = {row.id: row.ml for row in rows}
    assert ml[low] > ml[high]  # reverse ml order: id order and ml order disagree
    await restock(container, low, 5)
    await restock(container, high, 5)
    perfumes = SqlPerfumeRepository(container.database)

    async def save_the_perfume() -> Ok[None]:
        perfume = await perfumes.get_for_update(perfume_id)
        assert perfume is not None
        assert [p.id for p in perfume.presentations] == [high, low]  # loaded by ml
        assert isinstance(await perfumes.save(perfume), Ok)
        return Ok(None)

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
        await scenario.reached(catalog.first_locked)  # the reserve holds the lower presentation
        save = scenario.spawn(run_in_transaction(container, save_the_perfume))

        assert await is_blocked(save)  # it needs the lower presentation the reserve share-locks
        release.set()
        reserve_result, _ = await scenario.finish(reserve, save)  # a deadlock raises here

    assert isinstance(reserve_result, Ok)
    assert await item_row(container, low) == (5, 1)
    assert await item_row(container, high) == (5, 1)
