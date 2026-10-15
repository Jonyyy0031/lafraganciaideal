"""Plan 002 on PostgreSQL: command -> outbox row -> relay -> `ApplyStockChanged` -> the copy on
`catalog.presentations` -> the storefront status. Crosses the inventory and catalog modules the
way production does: only through the outbox and the event name."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.queries.perfumes import GetPublicPerfume
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.infrastructure.tables import presentations
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import StockChanged
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import OutboxEventPublisher, outbox
from fragancia_api.shared.kernel import Err, Ok, new_id
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration

EVENT = "inventory.stock.changed"
MADE_TO_ORDER = {"availability": "made_to_order", "lead_time_min_days": 7, "lead_time_max_days": 10}


async def restock(container: Container, presentation_id: UUID, quantity: int) -> None:
    result = await container.services.get(RecordMovement).execute(
        presentation_id, MovementRequest(kind="restock", quantity=quantity, reason="Compra"), "a"
    )
    assert isinstance(result, Ok)


async def lose(container: Container, presentation_id: UUID, quantity: int) -> None:
    result = await container.services.get(RecordMovement).execute(
        presentation_id, MovementRequest(kind="loss", quantity=quantity, reason="Rotura"), "a"
    )
    assert isinstance(result, Ok)


async def outbox_rows(container: Container) -> list[dict[str, Any]]:
    async with container.database.reader() as session:
        rows = (await session.execute(select(outbox).order_by(outbox.c.occurred_at))).mappings()
        return [dict(row) for row in rows]


async def copy_of(container: Container, presentation_id: UUID) -> tuple[int, int, datetime | None]:
    async with container.database.reader() as session:
        row = (
            await session.execute(
                select(
                    presentations.c.stock_available,
                    presentations.c.stock_threshold,
                    presentations.c.stock_as_of,
                ).where(presentations.c.id == presentation_id)
            )
        ).one()
    return row.stock_available, row.stock_threshold, row.stock_as_of


async def relay_all(container: Container) -> None:
    while await container.relay.relay_batch():
        pass


async def publish_stock_changed(
    container: Container, presentation_id: UUID, available: int, threshold: int, as_of: datetime
) -> None:
    runner = SqlTransactionRunner(container.database)
    publisher = OutboxEventPublisher(container.database)

    async def work() -> Ok[None]:
        await publisher.publish(
            [
                StockChanged(
                    presentation_id=presentation_id,
                    available=available,
                    threshold=threshold,
                    as_of=as_of,
                )
            ]
        )
        return Ok(None)

    await runner.run(work)


async def statuses(container: Container, perfume_id: UUID) -> dict[int, str | None]:
    """The storefront's `stock_status` of each size of the perfume, by ml."""
    result = await container.services.get(GetPublicPerfume).execute(
        f"{slugify('Eros')}-{perfume_id}"
    )
    assert isinstance(result, Ok)
    return {p.ml: p.stock_status for p in result.value.presentations}


# --- the outbox row ----------------------------------------------------------------------------


async def test_a_restock_puts_one_stock_changed_row_in_the_outbox(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()

    await restock(container, presentation_id, 10)

    [row] = await outbox_rows(container)
    assert row["name"] == EVENT
    assert row["published_at"] is None
    payload = row["payload"]
    assert set(payload) == {"presentation_id", "available", "threshold", "as_of"}
    assert payload["presentation_id"] == str(presentation_id)
    assert (payload["available"], payload["threshold"]) == (10, 3)
    assert datetime.fromisoformat(payload["as_of"]).tzinfo is not None


async def test_a_refused_movement_leaves_no_outbox_row(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()

    result = await container.services.get(RecordMovement).execute(
        presentation_id, MovementRequest(kind="loss", quantity=1, reason="Rotura"), "a"
    )

    assert isinstance(result, Err)
    assert await outbox_rows(container) == []


async def test_each_command_appends_its_own_event_in_order(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await lose(container, presentation_id, 2)
    await container.services.get(SetThreshold).execute(presentation_id, 8)

    rows = [r for r in await outbox_rows(container) if r["name"] == EVENT]

    assert [(r["payload"]["available"], r["payload"]["threshold"]) for r in rows] == [
        (5, 3),
        (3, 3),
        (3, 8),
    ]


# --- relay -> handler -> copy -> storefront ----------------------------------------------------


async def test_a_restock_reaches_the_copy_and_the_detail_through_the_relay(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume(sizes=({"stock_available": 0},))
    assert await copy_of(container, presentation_id) == (0, 3, None)
    assert await statuses(container, perfume_id) == {100: "sold_out"}

    await restock(container, presentation_id, 10)
    assert await copy_of(container, presentation_id) == (0, 3, None)  # nothing before the relay
    await relay_all(container)

    available, threshold, as_of = await copy_of(container, presentation_id)
    assert (available, threshold) == (10, 3)
    assert as_of is not None
    assert await statuses(container, perfume_id) == {100: "available"}
    [row] = await outbox_rows(container)
    assert row["published_at"] is not None


async def test_every_status_follows_the_movements_on_the_detail_page(
    container: Container, catalog_rows: CatalogRows
) -> None:
    # 50 ml is counted by the movements, 75 ml is never counted, 100 ml is made to order.
    perfume_id, [counted, *_] = await catalog_rows.perfume(
        sizes=({"ml": 50}, {"ml": 75}, {"ml": 100, **MADE_TO_ORDER})
    )

    await restock(container, counted, 10)
    await relay_all(container)
    assert await statuses(container, perfume_id) == {50: "available", 75: "sold_out", 100: None}

    await lose(container, counted, 7)  # 10 - 7 = 3: equal to the threshold
    await relay_all(container)
    assert await statuses(container, perfume_id) == {50: "last_units", 75: "sold_out", 100: None}

    await restock(container, counted, 1)  # 4: one above the threshold
    await relay_all(container)
    assert (await statuses(container, perfume_id))[50] == "available"

    await lose(container, counted, 4)  # 0
    await relay_all(container)
    assert await statuses(container, perfume_id) == {50: "sold_out", 75: "sold_out", 100: None}


async def test_a_threshold_override_above_the_stock_shows_last_units(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await relay_all(container)
    assert await statuses(container, perfume_id) == {100: "available"}

    result = await container.services.get(SetThreshold).execute(presentation_id, 15)
    await relay_all(container)

    assert isinstance(result, Ok)
    available, threshold, _ = await copy_of(container, presentation_id)
    assert (available, threshold) == (10, 15)
    assert await statuses(container, perfume_id) == {100: "last_units"}

    await container.services.get(SetThreshold).execute(presentation_id, None)
    await relay_all(container)
    assert await statuses(container, perfume_id) == {100: "available"}


# --- global settings change --------------------------------------------------------------------


async def test_a_global_threshold_change_republishes_one_event_per_stock_item(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [plain, overridden, other] = await catalog_rows.perfume(
        sizes=({"ml": 50}, {"ml": 75}, {"ml": 100})
    )
    await restock(container, plain, 10)
    await restock(container, overridden, 10)
    await restock(container, other, 2)  # already low at the global 3
    await container.services.get(SetThreshold).execute(overridden, 2)
    await relay_all(container)
    assert await statuses(container, perfume_id) == {
        50: "available",
        75: "available",
        100: "last_units",
    }
    assert await outbox_rows(container) != []

    result = await container.services.get(UpdateInventorySettings).execute(12)
    new_rows = [
        r for r in await outbox_rows(container) if r["published_at"] is None and r["name"] == EVENT
    ]

    assert isinstance(result, Ok)
    thresholds = {UUID(r["payload"]["presentation_id"]): r["payload"] for r in new_rows}
    assert len(new_rows) == 3 and set(thresholds) == {plain, overridden, other}
    assert (thresholds[plain]["available"], thresholds[plain]["threshold"]) == (10, 12)
    assert (thresholds[overridden]["available"], thresholds[overridden]["threshold"]) == (10, 2)
    assert (thresholds[other]["available"], thresholds[other]["threshold"]) == (2, 12)

    await relay_all(container)

    # Only the item without an override moved from "available" to "last units".
    assert await statuses(container, perfume_id) == {
        50: "last_units",
        75: "available",
        100: "last_units",
    }


async def test_a_global_threshold_change_with_no_stock_items_publishes_nothing(
    container: Container,
) -> None:
    result = await container.services.get(UpdateInventorySettings).execute(7)

    assert isinstance(result, Ok)
    assert await outbox_rows(container) == []


# --- idempotent, newest wins -------------------------------------------------------------------


async def test_an_older_event_is_ignored_and_the_newer_copy_stays(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await relay_all(container)
    _, _, newest = await copy_of(container, presentation_id)
    assert newest is not None

    await publish_stock_changed(container, presentation_id, 0, 3, newest - timedelta(hours=1))
    await relay_all(container)

    assert await copy_of(container, presentation_id) == (10, 3, newest)
    assert await statuses(container, perfume_id) == {100: "available"}
    assert all(r["published_at"] is not None for r in await outbox_rows(container))


async def test_replaying_the_same_event_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await relay_all(container)
    _, _, as_of = await copy_of(container, presentation_id)
    assert as_of is not None

    await publish_stock_changed(container, presentation_id, 99, 50, as_of)
    await relay_all(container)

    assert await copy_of(container, presentation_id) == (10, 3, as_of)


async def test_events_delivered_out_of_order_keep_the_newest(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    t1 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    t2 = t1 + timedelta(minutes=5)

    await publish_stock_changed(container, presentation_id, 7, 3, t2)
    await relay_all(container)
    await publish_stock_changed(container, presentation_id, 1, 3, t1)
    await relay_all(container)

    assert await copy_of(container, presentation_id) == (7, 3, t2)


async def test_an_event_for_an_unknown_presentation_is_a_published_no_op(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()

    await publish_stock_changed(container, new_id(), 5, 3, datetime(2026, 10, 5, tzinfo=UTC))
    await relay_all(container)

    assert await copy_of(container, presentation_id) == (0, 3, None)
    [row] = await outbox_rows(container)
    assert row["published_at"] is not None


async def test_a_malformed_event_is_recorded_as_a_failure_and_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(outbox).values(
                id=new_id(),
                name=EVENT,
                payload={
                    "presentation_id": str(presentation_id),
                    "available": "many",
                    "threshold": 3,
                    "as_of": "2026-10-05T09:00:00+00:00",
                },
                occurred_at=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
            )
        )

    await container.relay.relay_batch()

    assert await copy_of(container, presentation_id) == (0, 3, None)
    [row] = await outbox_rows(container)
    assert row["published_at"] is None
    assert row["attempts"] == 1
    assert "invalid literal" in str(row["last_error"])
