"""Plan 002, repair round 1 (review M1) on PostgreSQL: a global threshold change and a stock
movement are serialized by the settings row lock, so the catalog copy never ends up behind
inventory. The races are deterministic: one side is paused (uncommitted, holding its locks) by a
gate while the other starts, and the test says when the gate opens.

A lock-order regression must fail these tests within seconds, never hang the suite: every gate
is opened and every task is finished or cancelled when a test leaves its `Scenario` block (pass
or fail), and every wait and every awaited task is bounded by `TIMEOUT`."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import InventorySettings, Threshold
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher
from fragancia_api.shared.kernel import DomainError, Ok, Result
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.test_stock_status_flow import (
    copy_of,
    outbox_rows,
    relay_all,
    restock,
    statuses,
)

pytestmark = pytest.mark.integration

TIMEOUT = 5  # every gate wait and every awaited task: a regression fails within seconds
WAIT = 0.5  # how long a call that must be blocked gets before the test calls it blocked


class Scenario:
    """Owns the gates and tasks of one test. Leaving the block, whether the body passed or an
    assertion failed, opens every gate and then finishes (or cancels) every task."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self._gates: list[asyncio.Event] = []
        self._tasks: list[asyncio.Task[Any]] = []

    async def __aenter__(self) -> Scenario:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for gate in self._gates:
            gate.set()
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=TIMEOUT)
            for task in still_running:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def gate(self) -> asyncio.Event:
        event = asyncio.Event()
        self._gates.append(event)
        return event

    def publisher(self) -> GatedPublisher:
        return GatedPublisher(self.container, self.gate())

    def spawn[T](self, coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(coroutine)
        self._tasks.append(task)
        return task

    async def reached(self, event: asyncio.Event) -> None:
        await asyncio.wait_for(event.wait(), timeout=TIMEOUT)

    async def finish(self, *tasks: asyncio.Task[Any]) -> list[Any]:
        return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=TIMEOUT))

    async def hold(
        self, work: Callable[[], Awaitable[None]]
    ) -> tuple[asyncio.Event, asyncio.Event]:
        """Run `work` in a transaction that then stays open until the returned release gate
        opens. Returns (`holding`, `release`); `holding` is set once `work` has run."""
        holding, release = asyncio.Event(), self.gate()

        async def paused() -> None:
            await work()
            holding.set()
            await asyncio.wait_for(release.wait(), timeout=2 * TIMEOUT)

        self.spawn(run_in_transaction(self.container, paused))
        await self.reached(holding)
        return holding, release


class GatedPublisher(OutboxEventPublisher):
    """Writes its events (uncommitted) and then pauses the transaction until released."""

    def __init__(self, container: Container, release: asyncio.Event) -> None:
        super().__init__(container.database)
        self.reached = asyncio.Event()
        self.release = release

    async def publish(self, events: Sequence[Any]) -> None:
        await super().publish(events)
        self.reached.set()
        await asyncio.wait_for(self.release.wait(), timeout=2 * TIMEOUT)


def record_command(container: Container, events: OutboxEventPublisher) -> RecordMovement:
    database = container.database
    return RecordMovement(
        stock=SqlStockRepository(database),
        settings=SqlSettingsRepository(database),
        catalog=CatalogPresentations(PresentationDirectory(SqlPresentationDirectory(database))),
        transactions=SqlTransactionRunner(database),
        events=events,
        clock=SystemClock(),
    )


def settings_command(container: Container, events: OutboxEventPublisher) -> UpdateInventorySettings:
    database = container.database
    return UpdateInventorySettings(
        settings=SqlSettingsRepository(database),
        stock=SqlStockRepository(database),
        transactions=SqlTransactionRunner(database),
        events=events,
        clock=SystemClock(),
    )


def plain(container: Container) -> OutboxEventPublisher:
    return OutboxEventPublisher(container.database)


def loss(quantity: int) -> MovementRequest:
    return MovementRequest(kind="loss", quantity=quantity, reason="Venta")


async def is_blocked(task: asyncio.Task[Any]) -> bool:
    done, _ = await asyncio.wait({task}, timeout=WAIT)
    return not done


# --- the regression: the sale of the last unit versus a global threshold change ---------------


async def test_a_settings_change_waits_for_a_movement_and_the_copy_ends_up_sold_out(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """The sale is stamped and uncommitted when the settings change starts. Before the repair the
    settings change read the old count (1) without waiting and stamped a LATER event, so after
    the sale committed the copy stayed at 1 while inventory said 0."""
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 1)
    await relay_all(container)
    before, threshold_before, _ = await copy_of(container, presentation_id)
    assert (before, threshold_before) == (1, 3)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        sale = scenario.spawn(
            record_command(container, gated).execute(presentation_id, loss(1), "a")
        )
        await scenario.reached(gated.reached)
        change = scenario.spawn(settings_command(container, plain(container)).execute(12))

        assert await is_blocked(change)  # it needs the settings row the sale holds in share mode
        gated.release.set()
        sale_result, change_result = await scenario.finish(sale, change)

    await relay_all(container)
    assert isinstance(sale_result, Ok) and isinstance(change_result, Ok)
    available, threshold, _ = await copy_of(container, presentation_id)
    assert (available, threshold) == (0, 12)
    assert await statuses(container, perfume_id) == {100: "sold_out"}
    # The settings event was stamped after the sale committed and carries its count.
    rows = [r["payload"] for r in await outbox_rows(container) if r["payload"]["threshold"] == 12]
    assert [(p["available"]) for p in rows] == [0]


async def test_a_movement_waits_for_a_settings_change_and_carries_the_new_threshold(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await relay_all(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        change = scenario.spawn(settings_command(container, gated).execute(12))
        await scenario.reached(gated.reached)
        sale = scenario.spawn(
            record_command(container, plain(container)).execute(presentation_id, loss(1), "a")
        )

        assert await is_blocked(sale)  # it needs the settings row the change holds for update
        gated.release.set()
        change_result, sale_result = await scenario.finish(change, sale)

    await relay_all(container)
    assert isinstance(change_result, Ok) and isinstance(sale_result, Ok)
    available, threshold, _ = await copy_of(container, presentation_id)
    assert (available, threshold) == (4, 12)  # 4 <= 12
    assert await statuses(container, perfume_id) == {100: "last_units"}


async def test_two_movements_do_not_block_each_other_on_the_settings_row(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [first, second] = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 100}))
    await restock(container, first, 5)
    await restock(container, second, 5)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        holding = scenario.spawn(record_command(container, gated).execute(first, loss(1), "a"))
        await scenario.reached(gated.reached)

        other = await asyncio.wait_for(
            record_command(container, plain(container)).execute(second, loss(1), "b"),
            timeout=TIMEOUT,
        )
        gated.release.set()
        [held_result] = await scenario.finish(holding)

    assert isinstance(other, Ok)
    assert isinstance(held_result, Ok)


# --- the settings change sees the committed movement's count -----------------------------------


async def test_the_settings_change_publishes_the_count_a_committed_movement_left(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await relay_all(container)
    sold = await asyncio.wait_for(
        record_command(container, plain(container)).execute(presentation_id, loss(8), "a"),
        timeout=TIMEOUT,
    )  # committed, not relayed yet
    assert isinstance(sold, Ok)

    result = await asyncio.wait_for(
        settings_command(container, plain(container)).execute(12), timeout=TIMEOUT
    )

    assert isinstance(result, Ok)
    pending = [r for r in await outbox_rows(container) if r["published_at"] is None]
    movement_event, settings_event = (r["payload"] for r in pending)
    assert (movement_event["available"], movement_event["threshold"]) == (2, 3)
    assert (settings_event["available"], settings_event["threshold"]) == (2, 12)
    assert pending[1]["payload"]["as_of"] > pending[0]["payload"]["as_of"]
    await relay_all(container)
    available, threshold, _ = await copy_of(container, presentation_id)
    assert (available, threshold) == (2, 12)
    assert await statuses(container, perfume_id) == {100: "last_units"}


# --- the SQL adapters --------------------------------------------------------------------------


async def run_in_transaction[T](container: Container, work: Callable[[], Awaitable[T]]) -> T:
    async def unit() -> Result[T, DomainError]:
        return Ok(await work())

    result = await SqlTransactionRunner(container.database).run(unit)
    assert isinstance(result, Ok)
    value: T = result.value
    return value


@pytest.fixture
async def settings_row_restored(container: Container):  # type: ignore[no-untyped-def]
    yield
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.settings (id, low_stock_threshold) VALUES (1, 3) "
                "ON CONFLICT (id) DO UPDATE SET low_stock_threshold = 3"
            )
        )


async def settings_rows(container: Container) -> list[int]:
    async with container.database.engine.connect() as connection:
        rows = await connection.execute(text("SELECT low_stock_threshold FROM inventory.settings"))
        return [row[0] for row in rows]


@pytest.mark.usefixtures("settings_row_restored")
@pytest.mark.parametrize("lock", ["get_for_update", "get_for_share"])
async def test_a_locked_read_inserts_the_default_settings_row_when_it_is_missing(
    container: Container, lock: str
) -> None:
    async with container.database.engine.begin() as connection:
        count = (
            await connection.execute(text("SELECT count(*) FROM inventory.settings"))
        ).scalar_one()
        assert count == 1  # proves the WHERE before the DELETE
        await connection.execute(text("DELETE FROM inventory.settings WHERE id = 1"))
    assert await settings_rows(container) == []
    repo = SqlSettingsRepository(container.database)

    value = await asyncio.wait_for(
        run_in_transaction(container, getattr(repo, lock)), timeout=TIMEOUT
    )

    assert value == InventorySettings(Threshold(3))
    assert await settings_rows(container) == [3]


@pytest.mark.parametrize("lock", ["get_for_update", "get_for_share"])
async def test_a_locked_read_returns_the_stored_value_and_does_not_overwrite_it(
    container: Container, lock: str
) -> None:
    repo = SqlSettingsRepository(container.database)
    await run_in_transaction(container, lambda: repo.save(InventorySettings(Threshold(8))))

    value = await asyncio.wait_for(
        run_in_transaction(container, getattr(repo, lock)), timeout=TIMEOUT
    )

    assert value == InventorySettings(Threshold(8))
    assert await settings_rows(container) == [8]


@pytest.mark.parametrize(
    ("held", "wanted", "blocks"),
    [
        ("get_for_update", "get_for_update", True),
        ("get_for_update", "get_for_share", True),
        ("get_for_share", "get_for_update", True),
        ("get_for_share", "get_for_share", False),
    ],
)
async def test_the_settings_row_lock_modes_conflict_as_documented(
    container: Container, held: str, wanted: str, blocks: bool
) -> None:
    repo = SqlSettingsRepository(container.database)

    async with Scenario(container) as scenario:

        async def take_held_lock() -> None:
            await getattr(repo, held)()

        _, release = await scenario.hold(take_held_lock)
        attempt = scenario.spawn(run_in_transaction(container, getattr(repo, wanted)))

        assert await is_blocked(attempt) is blocks
        release.set()
        await scenario.finish(attempt)


async def test_list_all_for_update_is_ordered_by_presentation_id_and_locks_every_item(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, ids = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 75}, {"ml": 100}))
    for presentation_id in ids:
        await restock(container, presentation_id, 3)
    repo = SqlStockRepository(container.database)
    seen: list[UUID] = []

    async with Scenario(container) as scenario:

        async def list_and_lock() -> None:
            seen.extend(item.presentation_id for item in await repo.list_all_for_update())

        _, release = await scenario.hold(list_and_lock)
        sale = scenario.spawn(
            record_command(container, plain(container)).execute(ids[0], loss(1), "a")
        )

        assert seen == sorted(ids)
        assert await is_blocked(sale)  # the movement needs an item row the listing holds
        release.set()
        [sale_result] = await scenario.finish(sale)

    assert isinstance(sale_result, Ok)
