"""Inventory persistence against `fragancia_test`: repository, locking, constraint, queries."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fragancia_api.container import Container
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.application.queries.stock import GetStock, ListStock
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.errors import PresentationNotFound
from fragancia_api.modules.inventory.domain.stock import (
    InventorySettings,
    Movement,
    MovementKind,
    Reason,
    StockItem,
    Threshold,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_queries import SqlStockQueries
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


async def in_transaction[T](container: Container, work: Callable[[], Awaitable[T]]) -> T:
    async def unit() -> Result[T, DomainError]:
        return Ok(await work())

    result = await SqlTransactionRunner(container.database).run(unit)
    assert isinstance(result, Ok)
    value: T = result.value
    return value


def restock(item: StockItem, quantity: int, *, at: datetime = NOW) -> Movement:
    result = item.record_movement(MovementKind.RESTOCK, quantity, Reason("Compra"), "actor-1", at)
    assert isinstance(result, Ok)
    return result.value


_COUNTS = {
    "inventory.stock_items": text("SELECT count(*) FROM inventory.stock_items"),
    "inventory.stock_movements": text("SELECT count(*) FROM inventory.stock_movements"),
    "inventory.settings": text("SELECT count(*) FROM inventory.settings"),
}


async def count(container: Container, table: str) -> int:
    async with container.database.engine.connect() as connection:
        return int((await connection.execute(_COUNTS[table])).scalar_one())


# --- repository --------------------------------------------------------------------------------


async def test_the_first_lock_creates_an_empty_item_that_save_then_fills(
    container: Container,
) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def first() -> None:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        assert (item.on_hand, item.threshold_override) == (0, None)
        restock(item, 10)
        await repo.save(item)
        assert item.new_movements == []

    await in_transaction(container, first)

    counted = await SqlStockQueries(container.database).get(presentation_id)
    assert counted == (10, None)
    assert await count(container, "inventory.stock_items") == 1
    assert await count(container, "inventory.stock_movements") == 1


async def test_a_second_load_returns_the_stored_units_override_and_time(
    container: Container,
) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def write() -> None:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        restock(item, 4, at=NOW)
        item.set_threshold(Threshold(25))
        await repo.save(item)

    async def read() -> StockItem | None:
        return await repo.get_for_update(presentation_id, now=NOW)

    await in_transaction(container, write)
    loaded = await in_transaction(container, read)

    assert loaded is not None
    assert (loaded.on_hand, loaded.threshold_override, loaded.updated_at) == (
        4,
        Threshold(25),
        NOW,
    )
    assert await count(container, "inventory.stock_items") == 1


async def test_save_stores_every_field_of_a_movement(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()
    moved: list[Movement] = []

    async def write() -> None:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        moved.append(restock(item, 10))
        result = item.record_movement(MovementKind.COUNT, 4, Reason("Conteo"), "actor-2", NOW)
        assert isinstance(result, Ok)
        moved.append(result.value)
        await repo.save(item)

    await in_transaction(container, write)

    async with container.database.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT id, kind, quantity, delta, on_hand_after, reason, actor_id, "
                        "created_at FROM inventory.stock_movements "
                        "WHERE presentation_id = :p ORDER BY on_hand_after DESC"
                    ),
                    {"p": presentation_id},
                )
            )
            .mappings()
            .all()
        )
    assert [
        (r["id"], r["kind"], r["quantity"], r["delta"], r["on_hand_after"], r["reason"])
        for r in rows
    ] == [
        (moved[0].id, "restock", 10, 10, 10, "Compra"),
        (moved[1].id, "count", 4, -6, 4, "Conteo"),
    ]
    assert rows[1]["actor_id"] == "actor-2"
    assert rows[0]["created_at"] == NOW


async def test_a_failed_transaction_leaves_neither_item_nor_movements(
    container: Container,
) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def work() -> Result[None, DomainError]:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        restock(item, 5)
        await repo.save(item)
        return Err(PresentationNotFound())  # the runner rolls back on Err

    result = await SqlTransactionRunner(container.database).run(work)

    assert isinstance(result, Err)
    assert await count(container, "inventory.stock_items") == 0
    assert await count(container, "inventory.stock_movements") == 0


async def test_two_concurrent_first_movements_serialize_on_the_row_lock(
    container: Container,
) -> None:
    """A holds the lock while it still has to save; B must wait, then add on top of A."""
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def movement(delay_after_lock: float) -> None:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        restock(item, 5)
        await asyncio.sleep(delay_after_lock)
        await repo.save(item)

    first = asyncio.create_task(in_transaction(container, lambda: movement(0.4)))
    await asyncio.sleep(0.1)
    second = asyncio.create_task(in_transaction(container, lambda: movement(0)))
    await asyncio.gather(first, second)

    assert await SqlStockQueries(container.database).get(presentation_id) == (10, None)
    assert await count(container, "inventory.stock_movements") == 2


async def test_two_concurrent_restocks_through_the_use_case_end_at_ten(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    record = container.services.get(RecordMovement)
    request = MovementRequest(kind="restock", quantity=5, reason="Compra")

    first, second = await asyncio.gather(
        record.execute(presentation_id, request, "actor-1"),
        record.execute(presentation_id, request, "actor-2"),
    )

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert sorted([first.value.on_hand, second.value.on_hand]) == [5, 10]
    assert await SqlStockQueries(container.database).get(presentation_id) == (10, None)
    movements = await SqlStockQueries(container.database).movements(
        presentation_id, page=1, size=10
    )
    assert movements.total == 2
    assert sorted(m.on_hand_after for m in movements.items) == [5, 10]
    assert {m.actor_id for m in movements.items} == {"actor-1", "actor-2"}


async def test_concurrent_losses_cannot_drive_the_stock_below_zero(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    record = container.services.get(RecordMovement)
    await record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=5, reason="Compra"), "a"
    )
    loss = MovementRequest(kind="loss", quantity=4, reason="Rotura")

    results = await asyncio.gather(
        record.execute(presentation_id, loss, "a"), record.execute(presentation_id, loss, "b")
    )

    outcomes = sorted("ok" if isinstance(r, Ok) else r.error.code for r in results)
    assert outcomes == ["INVENTORY_STOCK_INSUFFICIENT", "ok"]
    assert await SqlStockQueries(container.database).get(presentation_id) == (1, None)


# --- check constraint --------------------------------------------------------------------------


async def test_the_database_refuses_a_negative_on_hand(container: Container) -> None:
    with pytest.raises(IntegrityError, match="ck_stock_items_on_hand_not_negative"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO inventory.stock_items (presentation_id, on_hand, updated_at) "
                    "VALUES (:p, -1, now())"
                ),
                {"p": new_id()},
            )
    assert await count(container, "inventory.stock_items") == 0


async def test_the_repository_cannot_save_a_negative_on_hand(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def write() -> None:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        item.on_hand = -1  # bypasses the aggregate on purpose: the constraint is the backstop
        await repo.save(item)

    with pytest.raises(IntegrityError, match="ck_stock_items_on_hand_not_negative"):
        await in_transaction(container, write)
    assert await count(container, "inventory.stock_items") == 0


async def test_an_update_to_a_negative_on_hand_is_refused_too(container: Container) -> None:
    presentation_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.stock_items (presentation_id, on_hand, updated_at) "
                "VALUES (:p, 2, now())"
            ),
            {"p": presentation_id},
        )

    with pytest.raises(IntegrityError, match="ck_stock_items_on_hand_not_negative"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE inventory.stock_items SET on_hand = -3 WHERE presentation_id = :p"),
                {"p": presentation_id},
            )
    assert await SqlStockQueries(container.database).get(presentation_id) == (2, None)


# --- SqlStockQueries ---------------------------------------------------------------------------


async def seed_movements(
    container: Container, presentation_id: UUID, rows: list[tuple[UUID, datetime, int]]
) -> None:
    async with container.database.engine.begin() as connection:
        for movement_id, created_at, quantity in rows:
            await connection.execute(
                text(
                    "INSERT INTO inventory.stock_movements (id, presentation_id, kind, quantity, "
                    "delta, on_hand_after, reason, actor_id, created_at) VALUES "
                    "(:id, :p, 'restock', :q, :q, :q, 'r', 'actor', :at)"
                ),
                {"id": movement_id, "p": presentation_id, "q": quantity, "at": created_at},
            )


async def test_movements_are_newest_first_ties_broken_by_id_desc(container: Container) -> None:
    presentation_id = new_id()
    low, high = UUID(int=1), UUID(int=2)
    older, newest = new_id(), new_id()
    await seed_movements(
        container,
        presentation_id,
        [
            (older, NOW - timedelta(hours=1), 1),
            (low, NOW, 2),
            (high, NOW, 3),
            (newest, NOW + timedelta(hours=1), 4),
        ],
    )

    page = await SqlStockQueries(container.database).movements(presentation_id, page=1, size=10)

    assert [m.id for m in page.items] == [newest, high, low, older]
    assert page.total == 4


async def test_movements_paginate_and_stay_within_one_presentation(container: Container) -> None:
    presentation_id, other = new_id(), new_id()
    ids = [UUID(int=n) for n in (1, 2, 3)]
    await seed_movements(
        container,
        presentation_id,
        [(ids[i], NOW + timedelta(minutes=i), i + 1) for i in range(3)],
    )
    await seed_movements(container, other, [(new_id(), NOW, 9)])
    queries = SqlStockQueries(container.database)

    first = await queries.movements(presentation_id, page=1, size=2)
    second = await queries.movements(presentation_id, page=2, size=2)
    beyond = await queries.movements(presentation_id, page=3, size=2)

    assert [m.id for m in first.items] == [ids[2], ids[1]]
    assert [m.id for m in second.items] == [ids[0]]
    assert beyond.items == []
    assert (first.total, second.total, beyond.total) == (3, 3, 3)
    assert (first.page, first.size) == (1, 2)


async def test_movements_of_a_presentation_without_any_are_empty(container: Container) -> None:
    page = await SqlStockQueries(container.database).movements(new_id(), page=1, size=10)

    assert (page.items, page.total) == ([], 0)


async def test_movement_fields_round_trip_through_the_contract(container: Container) -> None:
    presentation_id = new_id()
    movement_id = new_id()
    await seed_movements(container, presentation_id, [(movement_id, NOW, 7)])

    [line] = (
        await SqlStockQueries(container.database).movements(presentation_id, page=1, size=10)
    ).items

    assert (line.id, line.kind, line.quantity, line.delta, line.on_hand_after) == (
        movement_id,
        "restock",
        7,
        7,
        7,
    )
    assert (line.reason, line.actor_id, line.created_at) == ("r", "actor", NOW)


async def test_get_and_on_hand_by_id(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    counted, with_override, never = new_id(), new_id(), new_id()

    async def write() -> None:
        first = await repo.get_for_update(counted, now=NOW)
        assert first is not None
        restock(first, 6)
        await repo.save(first)
        second = await repo.get_for_update(with_override, now=NOW)
        assert second is not None
        second.set_threshold(Threshold(0))
        await repo.save(second)

    await in_transaction(container, write)
    queries = SqlStockQueries(container.database)

    assert await queries.get(counted) == (6, None)
    assert await queries.get(never) is None
    assert await queries.on_hand_by_id([counted, with_override, never]) == {
        counted: (6, None),
        with_override: (0, 0),
    }
    assert await queries.on_hand_by_id([]) == {}


# --- settings ----------------------------------------------------------------------------------


async def test_the_seeded_global_threshold_is_three(container: Container) -> None:
    async with container.database.engine.connect() as connection:
        rows = (
            await connection.execute(text("SELECT id, low_stock_threshold FROM inventory.settings"))
        ).all()

    assert [tuple(r) for r in rows] == [(1, 3)]
    assert await SqlStockQueries(container.database).low_stock_threshold() == 3


async def test_the_settings_repository_reads_and_upserts_the_single_row(
    container: Container,
) -> None:
    repo = SqlSettingsRepository(container.database)

    async def change() -> InventorySettings:
        await repo.save(InventorySettings(low_stock_threshold=Threshold(8)))
        return await repo.get()

    assert await in_transaction(container, change) == InventorySettings(Threshold(8))
    assert await SqlStockQueries(container.database).low_stock_threshold() == 8
    assert await count(container, "inventory.settings") == 1


async def test_update_inventory_settings_use_case_persists_the_threshold(
    container: Container,
) -> None:
    result = await container.services.get(UpdateInventorySettings).execute(5)

    assert isinstance(result, Ok)
    assert await SqlStockQueries(container.database).low_stock_threshold() == 5


# --- use cases over the real catalog and database ---------------------------------------------


async def test_the_stock_list_joins_catalog_presentations_with_counted_stock(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [counted, never, made_to_order] = await catalog_rows.perfume(
        "Eros",
        brand="Zz Versace",
        sizes=(
            {"ml": 50},
            {"ml": 100},
            {
                "ml": 200,
                "availability": "made_to_order",
                "lead_time_min_days": 3,
                "lead_time_max_days": 5,
            },
        ),
    )
    await catalog_rows.perfume("Old", brand="Zz Versace", archived=True)
    record = container.services.get(RecordMovement)
    await record.execute(
        counted, MovementRequest(kind="restock", quantity=10, reason="Compra"), "a"
    )

    everything = await container.services.get(ListStock).execute(low=False, page=1, size=20)
    low = await container.services.get(ListStock).execute(low=True, page=1, size=20)

    assert [(line.presentation_id, line.on_hand, line.is_low) for line in everything.items] == [
        (counted, 10, False),
        (never, 0, True),
    ]
    assert everything.items[0].label == "Zz Versace Eros ZRE 50 ml"
    assert made_to_order not in {line.presentation_id for line in everything.items}
    assert [line.presentation_id for line in low.items] == [never]
    assert low.total == 1


async def test_threshold_override_makes_a_counted_presentation_low(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await container.services.get(RecordMovement).execute(
        presentation_id, MovementRequest(kind="restock", quantity=20, reason="Compra"), "a"
    )
    set_threshold = container.services.get(SetThreshold)

    await set_threshold.execute(presentation_id, 25)
    low = await container.services.get(GetStock).execute(presentation_id)
    await set_threshold.execute(presentation_id, None)
    cleared = await container.services.get(GetStock).execute(presentation_id)

    assert isinstance(low, Ok)
    assert (low.value.on_hand, low.value.effective_threshold, low.value.is_low) == (20, 25, True)
    assert isinstance(cleared, Ok)
    assert (cleared.value.threshold_override, cleared.value.effective_threshold) == (None, 3)
    assert cleared.value.is_low is False


async def test_a_movement_on_a_made_to_order_presentation_writes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume(
        sizes=({"availability": "made_to_order", "lead_time_min_days": 3, "lead_time_max_days": 5},)
    )

    result = await container.services.get(RecordMovement).execute(
        presentation_id, MovementRequest(kind="restock", quantity=1, reason="x"), "a"
    )

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"
    assert await count(container, "inventory.stock_items") == 0
