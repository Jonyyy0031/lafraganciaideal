"""Plan 005 on PostgreSQL: the Excel import's stock counts, from the confirm to the ledger.

Confirm (catalog) -> `catalog.import.applied` row in the outbox -> relay -> `ApplyImportedCounts`
-> count movements (inventory) -> `inventory.stock.changed` rows -> the catalog copy. The two
modules meet only through the outbox and the event name, as in production. Ready imports are
stored with the real clock, because the container's `ConfirmCatalogImport` reads it."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog.testing
from sqlalchemy import select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ConfirmCatalogImport,
)
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportPlan,
    ImportSummary,
    PlannedPerfume,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.perfume import Gender
from fragancia_api.modules.catalog.infrastructure.sql_catalog_import_repository import (
    SqlCatalogImportRepository,
)
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.modules.catalog.infrastructure.tables import imports, presentations
from fragancia_api.modules.inventory.application.commands.stock import (
    IMPORT_COUNT_REASON,
    RecordImportedCounts,
)
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.modules.inventory.infrastructure.tables import stock_items, stock_movements
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher
from fragancia_api.shared.kernel import Ok, new_id
from tests.integration.catalog.import_fixtures import (
    World,
    seed_perfume,
    world,  # noqa: F401  (a fixture)
)
from tests.integration.inventory.test_stock_settings_race import (
    TIMEOUT,
    Scenario,
    is_blocked,
    loss,
    plain,
    record_command,
    settings_command,
)
from tests.integration.inventory.test_stock_status_flow import (
    copy_of,
    outbox_rows,
    relay_all,
    restock,
)

pytestmark = pytest.mark.integration

ACTOR = "admin-counts"
APPLIED = "catalog.import.applied"
CHANGED = "inventory.stock.changed"


# --- helpers ----------------------------------------------------------------------------------


def _presentation(
    ml: int,
    row: int,
    *,
    presentation_id: UUID | None = None,
    stock_count: int | None = None,
) -> PlannedPresentation:
    return PlannedPresentation(
        presentation_id, ml, 250_000, None, None, None, "in_stock", None, None, row, stock_count
    )


def _planned(
    world: World,  # noqa: F811
    perfume_id: UUID,
    name: str,
    *,
    is_new: bool,
    first_row: int,
    presentations_: tuple[PlannedPresentation, ...],
) -> PlannedPerfume:
    return PlannedPerfume(
        perfume_id=perfume_id,
        is_new=is_new,
        brand_id=world.brand,
        concentration_id=world.concentration,
        family_id=world.family,
        name=name,
        gender=Gender.WOMEN,
        description="Desde el Excel",
        top_notes=("Lima",),
        heart_notes=(),
        base_notes=(),
        publish=None,
        first_row=first_row,
        presentations=presentations_,
        photos=(),
    )


async def _store_ready(container: Container, *planned: PlannedPerfume) -> UUID:
    now = datetime.now(UTC)
    counted = sum(
        1 for perfume in planned for p in perfume.presentations if p.stock_count is not None
    )
    item = CatalogImport.create(b"PK\x03\x04data", "admin-it", now - timedelta(minutes=5))
    item.start(now - timedelta(minutes=4))
    item.finish_ready(
        now - timedelta(minutes=3),
        timedelta(minutes=63),
        len(planned),
        ImportSummary(len(planned), 0, 0, 0, 0, stock_counts=counted),
        ImportPlan(planned),
    )
    repository = SqlCatalogImportRepository(container.database)

    async def work() -> Ok[None]:
        await repository.add(item)
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)
    return item.id


async def _presentation_id(container: Container, perfume_id: UUID, ml: int) -> UUID:
    async with container.database.reader() as session:
        found = await session.scalar(
            select(presentations.c.id).where(
                presentations.c.perfume_id == perfume_id, presentations.c.ml == ml
            )
        )
    assert found is not None
    return UUID(str(found))


async def _movements(container: Container, presentation_id: UUID) -> list[dict[str, Any]]:
    async with container.database.reader() as session:
        rows = await session.execute(
            select(stock_movements)
            .where(stock_movements.c.presentation_id == presentation_id)
            .order_by(stock_movements.c.created_at, stock_movements.c.id)
        )
        return [dict(row) for row in rows.mappings()]


async def _on_hand(container: Container, presentation_id: UUID) -> int | None:
    async with container.database.reader() as session:
        found = await session.scalar(
            select(stock_items.c.on_hand).where(stock_items.c.presentation_id == presentation_id)
        )
    return None if found is None else int(found)


async def _rows_named(container: Container, name: str) -> list[dict[str, Any]]:
    return [row for row in await outbox_rows(container) if row["name"] == name]


async def _forget_the_outbox(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE platform.outbox"))


async def _confirm(container: Container, import_id: UUID) -> None:
    result = await container.services.get(ConfirmCatalogImport).execute(
        import_id, ACTOR, may_record_stock=True
    )
    assert isinstance(result, Ok), result


def _counts_command(container: Container, events: OutboxEventPublisher) -> RecordImportedCounts:
    database = container.database
    return RecordImportedCounts(
        stock=SqlStockRepository(database),
        settings=SqlSettingsRepository(database),
        catalog=CatalogPresentations(PresentationDirectory(SqlPresentationDirectory(database))),
        transactions=SqlTransactionRunner(database),
        events=events,
        clock=SystemClock(),
    )


# --- confirm -> outbox -> relay -> ledger ------------------------------------------------------


async def test_confirming_counts_puts_one_event_in_the_outbox_with_the_final_presentation_ids(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    existing = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(100,))
    existing_presentation = await _presentation_id(container, existing, 100)
    new = new_id()
    import_id = await _store_ready(
        container,
        _planned(
            world,
            existing,
            "Zz Cnt A",
            is_new=False,
            first_row=2,
            presentations_=(
                _presentation(100, 2, presentation_id=existing_presentation, stock_count=4),
            ),
        ),
        _planned(
            world,
            new,
            "Zz Cnt B",
            is_new=True,
            first_row=3,
            presentations_=(
                _presentation(100, 3, stock_count=12),
                _presentation(50, 4, stock_count=0),
                _presentation(30, 5),  # an empty cell: no count
            ),
        ),
    )

    await _confirm(container, import_id)

    [row] = await _rows_named(container, APPLIED)
    assert row["published_at"] is None
    new_100 = await _presentation_id(container, new, 100)
    new_50 = await _presentation_id(container, new, 50)
    assert row["payload"] == {
        "import_id": str(import_id),
        "actor_id": ACTOR,
        "stock_counts": {str(existing_presentation): 4, str(new_100): 12, str(new_50): 0},
    }
    assert await _rows_named(container, CHANGED) == []  # inventory has not run yet
    assert await _on_hand(container, new_100) is None


async def test_the_relay_turns_the_counts_into_count_movements_stock_events_and_the_copy(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    existing = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(100,))
    existing_presentation = await _presentation_id(container, existing, 100)
    await restock(container, existing_presentation, 7)
    await relay_all(container)
    await _forget_the_outbox(container)
    new = new_id()
    import_id = await _store_ready(
        container,
        _planned(
            world,
            existing,
            "Zz Cnt A",
            is_new=False,
            first_row=2,
            presentations_=(
                _presentation(100, 2, presentation_id=existing_presentation, stock_count=4),
            ),
        ),
        _planned(
            world,
            new,
            "Zz Cnt B",
            is_new=True,
            first_row=3,
            presentations_=(
                _presentation(100, 3, stock_count=12),
                _presentation(50, 4, stock_count=0),
                _presentation(30, 5),
            ),
        ),
    )
    await _confirm(container, import_id)
    new_100 = await _presentation_id(container, new, 100)
    new_50 = await _presentation_id(container, new, 50)
    new_30 = await _presentation_id(container, new, 30)

    await relay_all(container)

    # Overwrite: 7 on hand and Existencias 4 ends at 4.
    last = (await _movements(container, existing_presentation))[-1]
    assert (last["kind"], last["quantity"], last["delta"], last["on_hand_after"]) == (
        "count",
        4,
        -3,
        4,
    )
    assert (last["reason"], last["actor_id"]) == (IMPORT_COUNT_REASON, ACTOR)
    assert await _on_hand(container, existing_presentation) == 4
    # New presentations: one count movement each, the first one creates the stock item.
    for presentation_id, count in ((new_100, 12), (new_50, 0)):
        [movement] = await _movements(container, presentation_id)
        assert (movement["kind"], movement["quantity"], movement["on_hand_after"]) == (
            "count",
            count,
            count,
        )
        assert (movement["reason"], movement["actor_id"]) == (IMPORT_COUNT_REASON, ACTOR)
        assert await _on_hand(container, presentation_id) == count
    # An empty cell records nothing.
    assert await _movements(container, new_30) == []
    assert await _on_hand(container, new_30) is None
    # One `inventory.stock.changed` per counted presentation, and the catalog copy follows.
    changed = await _rows_named(container, CHANGED)
    assert sorted(r["payload"]["presentation_id"] for r in changed) == sorted(
        str(p) for p in (existing_presentation, new_100, new_50)
    )
    assert (await copy_of(container, existing_presentation))[:2] == (4, 3)
    assert (await copy_of(container, new_100))[:2] == (12, 3)
    assert (await copy_of(container, new_50))[:2] == (0, 3)
    # The delivery is marked published, so another relay pass repeats nothing.
    [applied] = await _rows_named(container, APPLIED)
    assert applied["published_at"] is not None
    await relay_all(container)
    assert len(await _movements(container, new_100)) == 1
    assert len(await _movements(container, existing_presentation)) == 2  # the restock, the count


async def test_a_count_equal_to_the_stock_is_recorded_with_delta_zero(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(100,))
    presentation_id = await _presentation_id(container, perfume, 100)
    await restock(container, presentation_id, 4)
    await relay_all(container)
    import_id = await _store_ready(
        container,
        _planned(
            world,
            perfume,
            "Zz Cnt A",
            is_new=False,
            first_row=2,
            presentations_=(_presentation(100, 2, presentation_id=presentation_id, stock_count=4),),
        ),
    )
    await _confirm(container, import_id)

    await relay_all(container)

    restock_movement, count_movement = await _movements(container, presentation_id)
    assert restock_movement["kind"] == "restock"
    assert (count_movement["kind"], count_movement["delta"], count_movement["on_hand_after"]) == (
        "count",
        0,
        4,
    )


async def test_an_import_without_counts_records_nothing_in_inventory(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(100,))
    presentation_id = await _presentation_id(container, perfume, 100)
    import_id = await _store_ready(
        container,
        _planned(
            world,
            perfume,
            "Zz Cnt A",
            is_new=False,
            first_row=2,
            presentations_=(_presentation(100, 2, presentation_id=presentation_id),),
        ),
    )
    await _confirm(container, import_id)

    [row] = await _rows_named(container, APPLIED)
    assert row["payload"]["stock_counts"] == {}
    await relay_all(container)

    assert await _movements(container, presentation_id) == []
    assert await _rows_named(container, CHANGED) == []
    [applied] = await _rows_named(container, APPLIED)
    assert applied["published_at"] is not None


async def test_a_stale_confirm_writes_no_event_and_no_count(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    archived = await seed_perfume(
        container, world, "Zz Cnt A", archived=True, presentation_ml=(100,)
    )
    presentation_id = await _presentation_id(container, archived, 100)
    import_id = await _store_ready(
        container,
        _planned(
            world,
            archived,
            "Zz Cnt A",
            is_new=False,
            first_row=2,
            presentations_=(_presentation(100, 2, presentation_id=presentation_id, stock_count=9),),
        ),
    )

    result = await container.services.get(ConfirmCatalogImport).execute(
        import_id, ACTOR, may_record_stock=True
    )

    assert not isinstance(result, Ok)
    assert result.error.code == "CATALOG_IMPORT_STALE"
    assert await outbox_rows(container) == []
    await relay_all(container)
    assert await _movements(container, presentation_id) == []
    async with container.database.reader() as session:
        status = await session.scalar(select(imports.c.status).where(imports.c.id == import_id))
    assert status == "ready"


async def test_a_presentation_switched_to_made_to_order_before_the_relay_is_skipped_with_a_log(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(50, 100))
    switched = await _presentation_id(container, perfume, 50)
    kept = await _presentation_id(container, perfume, 100)
    import_id = await _store_ready(
        container,
        _planned(
            world,
            perfume,
            "Zz Cnt A",
            is_new=False,
            first_row=2,
            presentations_=(
                _presentation(50, 2, presentation_id=switched, stock_count=5),
                _presentation(100, 3, presentation_id=kept, stock_count=8),
            ),
        ),
    )
    await _confirm(container, import_id)
    async with container.database.engine.begin() as connection:
        found = (
            await connection.execute(
                text("SELECT count(*) FROM catalog.presentations WHERE id = :id"),
                {"id": switched},
            )
        ).scalar_one()
        assert found == 1  # proves the WHERE before the UPDATE
        await connection.execute(
            text(
                "UPDATE catalog.presentations SET availability = 'made_to_order', "
                "lead_time_min_days = 3, lead_time_max_days = 5 WHERE id = :id"
            ),
            {"id": switched},
        )

    with structlog.testing.capture_logs() as logs:
        await relay_all(container)

    skipped = [log for log in logs if log["event"] == "inventory.import_count_skipped"]
    assert [(log["presentation_id"], log["code"]) for log in skipped] == [
        (str(switched), "INVENTORY_PRESENTATION_NOT_STOCKED")
    ]
    assert await _movements(container, switched) == []
    assert await _on_hand(container, kept) == 8
    [applied] = await _rows_named(container, APPLIED)
    assert applied["published_at"] is not None


# --- the lock order (the count joins the movements' order: catalog, settings, item) ------------


async def test_the_counts_wait_for_a_movement_in_flight_and_the_count_ends_the_ledger(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(50, 100))
    first = await _presentation_id(container, perfume, 50)
    second = await _presentation_id(container, perfume, 100)
    await restock(container, first, 5)
    await restock(container, second, 5)
    await relay_all(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        sale = scenario.spawn(record_command(container, gated).execute(first, loss(1), "shop"))
        await scenario.reached(gated.reached)
        counting = scenario.spawn(
            _counts_command(container, plain(container)).execute({second: 9, first: 2}, ACTOR)
        )

        assert await is_blocked(counting)  # it needs the item the sale holds
        gated.release.set()
        sale_result, counting_result = await scenario.finish(sale, counting)

    assert isinstance(sale_result, Ok)
    assert counting_result == Ok(2)
    assert [m["kind"] for m in await _movements(container, first)] == ["restock", "loss", "count"]
    assert await _on_hand(container, first) == 2  # the count came after the sale and won
    assert await _on_hand(container, second) == 9


async def test_a_movement_on_a_later_item_does_not_wait_for_counts_holding_an_earlier_one(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(50, 100))
    ids = sorted([await _presentation_id(container, perfume, ml) for ml in (50, 100)])
    low, high = ids
    await restock(container, low, 5)
    await restock(container, high, 5)
    await relay_all(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        counting = scenario.spawn(
            _counts_command(container, gated).execute({high: 1, low: 2}, ACTOR)
        )
        await scenario.reached(gated.reached)  # `low` is counted and held; `high` is not locked yet

        sale_of_high = await asyncio.wait_for(
            record_command(container, plain(container)).execute(high, loss(1), "shop"),
            timeout=TIMEOUT,
        )
        sale_of_low = scenario.spawn(
            record_command(container, plain(container)).execute(low, loss(1), "shop")
        )

        assert await is_blocked(sale_of_low)  # `low` is held by the counts
        gated.release.set()
        counting_result, low_result = await scenario.finish(counting, sale_of_low)

    assert isinstance(sale_of_high, Ok)
    assert isinstance(low_result, Ok)
    assert counting_result == Ok(2)
    # No deadlock, and every ledger is consistent: the count then the sale of `low`, and the
    # sale of `high` then the count of `high`.
    assert [m["kind"] for m in await _movements(container, low)] == ["restock", "count", "loss"]
    assert [m["kind"] for m in await _movements(container, high)] == ["restock", "loss", "count"]
    assert await _on_hand(container, low) == 1
    assert await _on_hand(container, high) == 1


async def test_the_counts_wait_for_a_settings_change_and_carry_the_new_threshold(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Zz Cnt A", presentation_ml=(100,))
    presentation_id = await _presentation_id(container, perfume, 100)
    await restock(container, presentation_id, 5)
    await relay_all(container)
    await _forget_the_outbox(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        change = scenario.spawn(settings_command(container, gated).execute(12))
        await scenario.reached(gated.reached)
        counting = scenario.spawn(
            _counts_command(container, plain(container)).execute({presentation_id: 6}, ACTOR)
        )

        assert await is_blocked(counting)  # it needs the settings row the change holds
        gated.release.set()
        change_result, counting_result = await scenario.finish(change, counting)

    assert isinstance(change_result, Ok)
    assert counting_result == Ok(1)
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[:2] == (6, 12)
