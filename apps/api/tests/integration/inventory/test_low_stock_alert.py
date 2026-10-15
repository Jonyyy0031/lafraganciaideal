"""Plan 004 on PostgreSQL: the low-stock alert state and its `inventory.stock.went_low` outbox
rows, written by the real commands over the real SQL adapters.

Covers `low_alerted` persisted by every repository read and by `save`, every command's outbox
row, and two deterministic races (a gate holds one transaction open while the other starts; every
gate is opened and every task finished or cancelled when a scenario ends, and each wait is
bounded by `TIMEOUT`, so a lock regression fails within seconds instead of hanging)."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.inventory.application.commands.reservations import (
    CommitReservation,
    ExpireReservations,
    ReleaseReservation,
)
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordImportedCounts,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import make_due, reserve_ok
from tests.integration.inventory.test_stock_settings_race import (
    TIMEOUT,
    Scenario,
    is_blocked,
    loss,
    plain,
    record_command,
    run_in_transaction,
    settings_command,
)
from tests.integration.inventory.test_stock_status_flow import (
    lose,
    outbox_rows,
    restock,
)

pytestmark = pytest.mark.integration

WENT_LOW = "inventory.stock.went_low"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


async def went_low_rows(container: Container) -> list[dict[str, Any]]:
    return [row for row in await outbox_rows(container) if row["name"] == WENT_LOW]


async def stored_flag(container: Container, presentation_id: UUID) -> bool:
    async with container.database.engine.connect() as connection:
        value = (
            await connection.execute(
                text("SELECT low_alerted FROM inventory.stock_items WHERE presentation_id = :id"),
                {"id": presentation_id},
            )
        ).scalar_one()
    return bool(value)


def line(presentation_id: UUID, on_hand: int, threshold: int = 3) -> dict[str, object]:
    return {"presentation_id": str(presentation_id), "on_hand": on_hand, "threshold": threshold}


# --- the column through the repository ---------------------------------------------------------


async def test_a_new_item_defaults_to_not_alerted_in_the_database(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)

    assert await stored_flag(container, presentation_id) is False


async def test_save_persists_the_flag_and_every_locked_read_returns_it(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [first, second] = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 100}))
    await restock(container, first, 10)
    await restock(container, second, 10)
    repository = SqlStockRepository(container.database)

    async def flag_first_only() -> None:
        item = await repository.get_for_update(first, now=NOW)
        assert item is not None and item.low_alerted is False
        item.low_alerted = True
        await repository.save(item)

    await run_in_transaction(container, flag_first_only)

    async def reads() -> tuple[bool, dict[UUID, bool], dict[UUID, bool]]:
        one = await repository.get_for_update(first, now=NOW)
        many = await repository.get_many_for_update([first, second], now=NOW)
        everything = await repository.list_all_for_update()
        assert one is not None
        return (
            one.low_alerted,
            {key: value.low_alerted for key, value in many.items()},
            {item.presentation_id: item.low_alerted for item in everything},
        )

    one, many, everything = await run_in_transaction(container, reads)

    assert one is True
    assert many == {first: True, second: False}
    assert everything == {first: True, second: False}
    assert await stored_flag(container, first) is True
    assert await stored_flag(container, second) is False


async def test_save_clears_the_flag(container: Container, catalog_rows: CatalogRows) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 2)
    assert await stored_flag(container, presentation_id) is True
    repository = SqlStockRepository(container.database)

    async def clear() -> None:
        item = await repository.get_for_update(presentation_id, now=NOW)
        assert item is not None
        item.low_alerted = False
        await repository.save(item)

    await run_in_transaction(container, clear)

    assert await stored_flag(container, presentation_id) is False


# --- RecordMovement ----------------------------------------------------------------------------


async def test_a_first_drop_to_the_threshold_leaves_one_went_low_row_with_that_line(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    assert await went_low_rows(container) == []

    await lose(container, presentation_id, 2)

    [row] = await went_low_rows(container)
    assert row["payload"] == {"lines": [line(presentation_id, 3)]}
    assert row["published_at"] is None
    assert await stored_flag(container, presentation_id) is True


async def test_a_further_loss_while_low_adds_no_row_and_a_restock_re_arms_it(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await lose(container, presentation_id, 2)

    await lose(container, presentation_id, 1)  # 2 on hand
    assert len(await went_low_rows(container)) == 1

    await restock(container, presentation_id, 4)  # 6 on hand
    assert await stored_flag(container, presentation_id) is False
    assert len(await went_low_rows(container)) == 1

    await lose(container, presentation_id, 3)  # 3 on hand again
    rows = await went_low_rows(container)
    assert [r["payload"] for r in rows] == [{"lines": [line(presentation_id, 3)]}] * 2


# --- reservations ------------------------------------------------------------------------------


async def test_reserving_and_expiring_leaves_no_went_low_row(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 4)
    reservation_id = await reserve_ok(container, {presentation_id: 1}, "QA-low-expire")
    await make_due(container, reservation_id)

    expired = await container.services.get(ExpireReservations).run_once()

    assert expired == 1
    assert await went_low_rows(container) == []
    assert await stored_flag(container, presentation_id) is False


async def test_releasing_a_reservation_leaves_no_went_low_row(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 4)
    reservation_id = await reserve_ok(container, {presentation_id: 1}, "QA-low-release")

    result = await container.services.get(ReleaseReservation).execute(reservation_id)

    assert isinstance(result, Ok)
    assert await went_low_rows(container) == []


async def test_a_sale_that_leaves_on_hand_at_the_threshold_leaves_one_went_low_row(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-low-sale")
    assert await went_low_rows(container) == []

    result = await container.services.get(CommitReservation).execute(reservation_id)

    assert isinstance(result, Ok)
    [row] = await went_low_rows(container)
    assert row["payload"] == {"lines": [line(presentation_id, 3)]}


# --- imports and thresholds --------------------------------------------------------------------


async def test_an_import_of_three_counts_at_zero_leaves_one_row_listing_all_three(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, ids = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 75}, {"ml": 100}))

    result = await container.services.get(RecordImportedCounts).execute(
        {presentation_id: 0 for presentation_id in ids}, "admin-low"
    )

    assert isinstance(result, Ok)
    [row] = await went_low_rows(container)
    assert sorted(row["payload"]["lines"], key=lambda item: item["presentation_id"]) == [
        line(presentation_id, 0) for presentation_id in sorted(ids)
    ]


async def test_raising_a_threshold_alerts_once_and_lowering_it_back_re_arms_silently(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 6)
    set_threshold = container.services.get(SetThreshold)

    await set_threshold.execute(presentation_id, 10)
    [row] = await went_low_rows(container)
    assert row["payload"] == {"lines": [line(presentation_id, 6, threshold=10)]}

    await set_threshold.execute(presentation_id, 3)

    assert len(await went_low_rows(container)) == 1
    assert await stored_flag(container, presentation_id) is False


async def test_a_global_threshold_change_alerts_the_items_it_covers_in_one_row_and_persists_it(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [plain_item, overridden, above] = await catalog_rows.perfume(
        sizes=({"ml": 50}, {"ml": 75}, {"ml": 100})
    )
    await restock(container, plain_item, 6)
    await restock(container, overridden, 6)
    await restock(container, above, 20)
    await container.services.get(SetThreshold).execute(overridden, 2)

    result = await container.services.get(UpdateInventorySettings).execute(10)

    assert isinstance(result, Ok)
    [row] = await went_low_rows(container)
    assert row["payload"] == {"lines": [line(plain_item, 6, threshold=10)]}
    assert await stored_flag(container, plain_item) is True
    assert await stored_flag(container, overridden) is False
    assert await stored_flag(container, above) is False

    await container.services.get(UpdateInventorySettings).execute(3)  # re-arms, no email

    assert len(await went_low_rows(container)) == 1
    assert await stored_flag(container, plain_item) is False


# --- races -------------------------------------------------------------------------------------


async def test_two_concurrent_losses_that_cross_the_threshold_alert_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """The first loss (5 -> 3) is stamped and uncommitted when the second starts: it waits on the
    item's row lock, then reads 3 with the item already alerted, so it adds no second row."""
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        first = scenario.spawn(
            record_command(container, gated).execute(presentation_id, loss(2), "a")
        )
        await scenario.reached(gated.reached)
        second = scenario.spawn(
            record_command(container, plain(container)).execute(presentation_id, loss(1), "b")
        )

        assert await is_blocked(second)  # it needs the item row the first one holds
        gated.release.set()
        first_result, second_result = await scenario.finish(first, second)

    assert isinstance(first_result, Ok) and isinstance(second_result, Ok)
    [row] = await went_low_rows(container)
    assert row["payload"] == {"lines": [line(presentation_id, 3)]}
    async with container.database.engine.connect() as connection:
        on_hand = (
            await connection.execute(
                text("SELECT on_hand FROM inventory.stock_items WHERE presentation_id = :id"),
                {"id": presentation_id},
            )
        ).scalar_one()
    assert on_hand == 2
    assert await stored_flag(container, presentation_id) is True


async def test_a_settings_change_and_a_movement_alert_the_same_item_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """The settings change (threshold 12, so 5 on hand is low) holds the item lock uncommitted
    when a loss starts; the loss waits, then sees the item already alerted."""
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        change = scenario.spawn(settings_command(container, gated).execute(12))
        await scenario.reached(gated.reached)
        sale = scenario.spawn(
            record_command(container, plain(container)).execute(presentation_id, loss(1), "a")
        )

        assert await is_blocked(sale)
        gated.release.set()
        change_result, sale_result = await scenario.finish(change, sale)

    assert isinstance(change_result, Ok) and isinstance(sale_result, Ok)
    [row] = await went_low_rows(container)
    assert row["payload"] == {"lines": [line(presentation_id, 5, threshold=12)]}
    assert await stored_flag(container, presentation_id) is True


async def test_the_race_helpers_time_out_instead_of_hanging(container: Container) -> None:
    """A gate nobody opens is bounded: leaving the scenario opens it and finishes its tasks."""
    async with Scenario(container) as scenario:
        never = scenario.gate()
        waiter = scenario.spawn(asyncio.wait_for(never.wait(), timeout=2 * TIMEOUT))

        assert await is_blocked(waiter)
    assert waiter.done()
