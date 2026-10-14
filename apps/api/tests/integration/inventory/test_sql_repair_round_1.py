"""Plan 001, repair round 1 against `fragancia_test`: L1 equality, L2 clock, L3 catalog lock."""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
)
from fragancia_api.modules.inventory.application.queries.stock import GetStock, ListStock
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration

AT = datetime(2031, 5, 6, 7, 8, tzinfo=UTC)
MADE_TO_ORDER = {"availability": "made_to_order", "lead_time_min_days": 3, "lead_time_max_days": 5}


def commands(container: Container) -> tuple[RecordMovement, SetThreshold]:
    """The real SQL adapters with a fixed clock (the container's own use cases use the system
    clock)."""
    database = container.database
    transactions = SqlTransactionRunner(database)
    catalog = CatalogPresentations(PresentationDirectory(SqlPresentationDirectory(database)))
    stock = SqlStockRepository(database)
    clock = FixedClock(AT)
    record = RecordMovement(
        stock=stock,
        settings=SqlSettingsRepository(database),
        catalog=catalog,
        transactions=transactions,
        events=OutboxEventPublisher(database),
        clock=clock,
    )
    return record, SetThreshold(
        stock=stock,
        settings=SqlSettingsRepository(database),
        catalog=catalog,
        transactions=transactions,
        events=OutboxEventPublisher(database),
        clock=clock,
    )


async def stored_updated_at(container: Container, presentation_id: object) -> datetime:
    async with container.database.engine.connect() as connection:
        value = (
            await connection.execute(
                text("SELECT updated_at FROM inventory.stock_items WHERE presentation_id = :p"),
                {"p": presentation_id},
            )
        ).scalar_one()
    assert isinstance(value, datetime)
    return value


# --- L1 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("on_hand", "override", "low"),
    [(3, None, True), (4, None, False), (5, 5, True), (6, 5, False), (3, 2, False)],
)
async def test_sql_backed_queries_flag_low_at_equality_like_the_domain(
    container: Container,
    catalog_rows: CatalogRows,
    on_hand: int,
    override: int | None,
    low: bool,
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    record, set_threshold = commands(container)
    await record.execute(
        presentation_id, MovementRequest(kind="count", quantity=on_hand, reason="x"), "a"
    )
    await set_threshold.execute(presentation_id, override)  # global threshold is 3

    got = await container.services.get(GetStock).execute(presentation_id)
    listed = await container.services.get(ListStock).execute(low=False, page=1, size=10)

    assert isinstance(got, Ok)
    assert got.value.is_low is low
    assert [line.is_low for line in listed.items] == [low]


# --- L2 ----------------------------------------------------------------------------------------


async def test_the_placeholder_row_is_stamped_with_the_given_clock(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def lock_only() -> Result[None, DomainError]:
        await repo.get_for_update(presentation_id, now=AT)
        return Ok(None)

    await SqlTransactionRunner(container.database).run(lock_only)

    assert await stored_updated_at(container, presentation_id) == AT


async def test_a_first_movement_and_a_first_threshold_keep_the_injected_clock(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [moved, thresholded] = await catalog_rows.perfume(sizes=({}, {}))
    record, set_threshold = commands(container)

    await record.execute(moved, MovementRequest(kind="restock", quantity=2, reason="x"), "a")
    await set_threshold.execute(thresholded, 4)

    assert await stored_updated_at(container, moved) == AT
    assert await stored_updated_at(container, thresholded) == AT


# --- L3 ----------------------------------------------------------------------------------------


async def test_find_for_update_raises_outside_a_transaction(container: Container) -> None:
    directory = SqlPresentationDirectory(container.database)

    with pytest.raises(RuntimeError, match="No active transaction"):
        await directory.find_for_update(new_id())


async def test_find_for_update_inside_a_transaction_reads_the_presentation(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    directory = SqlPresentationDirectory(container.database)
    seen: list[object] = []

    async def work() -> Result[None, DomainError]:
        seen.append(await directory.find_for_update(presentation_id))
        seen.append(await directory.find_for_update(new_id()))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    found, unknown = seen
    assert getattr(found, "presentation_id", None) == presentation_id
    assert unknown is None


type Command = Callable[[Container, object], Coroutine[Any, Any, Result[object, DomainError]]]


async def _record(container: Container, presentation_id: object) -> Result[object, DomainError]:
    record, _ = commands(container)
    return await record.execute(
        presentation_id,  # type: ignore[arg-type]
        MovementRequest(kind="restock", quantity=1, reason="x"),
        "a",
    )


async def _threshold(container: Container, presentation_id: object) -> Result[object, DomainError]:
    _, set_threshold = commands(container)
    return await set_threshold.execute(presentation_id, 5)  # type: ignore[arg-type]


@pytest.mark.parametrize("command", [_record, _threshold], ids=["movement", "threshold"])
async def test_a_command_waits_for_a_catalog_transaction_and_sees_the_switch_to_made_to_order(
    container: Container, catalog_rows: CatalogRows, command: Command
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()

    async with container.database.engine.connect() as catalog_tx:
        await catalog_tx.execute(
            text("SELECT id FROM catalog.presentations WHERE id = :p FOR UPDATE"),
            {"p": presentation_id},
        )
        task = asyncio.create_task(command(container, presentation_id))
        done, pending = await asyncio.wait({task}, timeout=0.7)
        assert (done, pending) == (set(), {task}), "the command should wait on the row lock"

        await catalog_tx.execute(
            text(
                "UPDATE catalog.presentations SET availability = 'made_to_order', "
                "lead_time_min_days = 3, lead_time_max_days = 5 WHERE id = :p"
            ),
            {"p": presentation_id},
        )
        await catalog_tx.commit()

    result = await asyncio.wait_for(task, timeout=10)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"
    async with container.database.engine.connect() as connection:
        items = (
            await connection.execute(text("SELECT count(*) FROM inventory.stock_items"))
        ).scalar_one()
        movements = (
            await connection.execute(text("SELECT count(*) FROM inventory.stock_movements"))
        ).scalar_one()
    assert (items, movements) == (0, 0)


async def test_a_command_proceeds_once_the_catalog_transaction_ends_unchanged(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()

    async with container.database.engine.connect() as catalog_tx:
        await catalog_tx.execute(
            text("SELECT id FROM catalog.presentations WHERE id = :p FOR UPDATE"),
            {"p": presentation_id},
        )
        task = asyncio.create_task(_record(container, presentation_id))
        await asyncio.sleep(0.3)
        assert not task.done()
        await catalog_tx.rollback()

    result = await asyncio.wait_for(task, timeout=10)

    assert isinstance(result, Ok)
