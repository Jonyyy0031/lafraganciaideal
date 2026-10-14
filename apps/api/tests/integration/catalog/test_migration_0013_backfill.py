"""Plan 002: migration 0013 (the catalog's copy of the stock figures) round trip and backfill,
run on `fragancia_test` only (`-x test=true`, the same switch as `just db-migrate --test`).

The migration is downgraded to 0012 and upgraded again inside the test, so the test must not run
in parallel with others (the suite is serial). Whatever happens, the `finally` brings the test
database back to `head`."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import insert, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.infrastructure.sql_stock_copy import SqlStockCopy
from fragancia_api.modules.inventory.infrastructure.tables import stock_items
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "PYTEST_XDIST_WORKER" in os.environ,
        reason="downgrades and upgrades the shared test database: cannot run under pytest-xdist",
    ),
]

API_ROOT = Path(__file__).resolve().parents[3]
COUNTED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
OVERRIDDEN_AT = datetime(2026, 9, 2, 11, 0, tzinfo=UTC)
COPY_COLUMNS = {"stock_available", "stock_threshold", "stock_as_of"}


async def alembic(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-x",
        "test=true",
        *args,
        cwd=API_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


async def copy_columns(container: Container) -> set[str]:
    async with container.database.engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'catalog' AND table_name = 'presentations'"
            )
        )
        return {row[0] for row in rows} & COPY_COLUMNS


async def copy_of(container: Container, presentation_id: UUID) -> tuple[int, int, datetime | None]:
    async with container.database.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT stock_available, stock_threshold, stock_as_of "
                    "FROM catalog.presentations WHERE id = :id"
                ),
                {"id": presentation_id},
            )
        ).one()
    return row[0], row[1], row[2]


@pytest.fixture
async def cleanup(container: Container) -> AsyncIterator[None]:
    yield
    await alembic("upgrade", "head")
    await container.database.engine.dispose()  # drop cached statements of the old table shape
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE inventory.stock_items, inventory.stock_movements"))
        await connection.execute(
            text(
                "INSERT INTO inventory.settings (id, low_stock_threshold) VALUES (1, 3) "
                "ON CONFLICT (id) DO UPDATE SET low_stock_threshold = 3"
            )
        )


async def seed(container: Container, catalog_rows: CatalogRows) -> tuple[UUID, UUID, UUID, UUID]:
    """counted (7 on hand), overridden (2 on hand, override 15), never counted, made to order."""
    _, [counted, overridden, never, made_to_order] = await catalog_rows.perfume(
        sizes=(
            {"ml": 50},
            {"ml": 75},
            {"ml": 100},
            {"ml": 200, "availability": "made_to_order", "lead_time_min_days": 7},
        )
    )
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(stock_items),
            [
                {
                    "presentation_id": counted,
                    "on_hand": 7,
                    "threshold_override": None,
                    "updated_at": COUNTED_AT,
                },
                {
                    "presentation_id": overridden,
                    "on_hand": 2,
                    "threshold_override": 15,
                    "updated_at": OVERRIDDEN_AT,
                },
            ],
        )
    return counted, overridden, never, made_to_order


@pytest.mark.usefixtures("cleanup")
async def test_the_migration_drops_and_restores_the_three_columns(
    container: Container, catalog_rows: CatalogRows
) -> None:
    assert await copy_columns(container) == COPY_COLUMNS

    await alembic("downgrade", "0012")
    after_downgrade = await copy_columns(container)
    await alembic("upgrade", "head")
    await container.database.engine.dispose()

    assert after_downgrade == set()
    assert await copy_columns(container) == COPY_COLUMNS


@pytest.mark.usefixtures("cleanup")
async def test_the_backfill_copies_counts_thresholds_and_timestamps(
    container: Container, catalog_rows: CatalogRows
) -> None:
    counted, overridden, never, made_to_order = await seed(container, catalog_rows)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE inventory.settings SET low_stock_threshold = 5 WHERE id = 1")
        )

    await alembic("downgrade", "0012")
    await alembic("upgrade", "head")
    await container.database.engine.dispose()

    assert await copy_of(container, counted) == (7, 5, COUNTED_AT)  # the global threshold
    assert await copy_of(container, overridden) == (2, 15, OVERRIDDEN_AT)  # its own override
    # Presentations never counted keep the column defaults: 0 available (sold out), no timestamp.
    assert await copy_of(container, never) == (0, 3, None)
    assert await copy_of(container, made_to_order) == (0, 3, None)


@pytest.mark.usefixtures("cleanup")
async def test_the_backfill_falls_back_to_threshold_3_when_the_settings_row_was_never_stored(
    container: Container, catalog_rows: CatalogRows
) -> None:
    counted, overridden, *_ = await seed(container, catalog_rows)
    async with container.database.engine.begin() as connection:
        before = (
            await connection.execute(text("SELECT count(*) FROM inventory.settings WHERE id = 1"))
        ).scalar_one()
        assert before == 1
        await connection.execute(text("DELETE FROM inventory.settings WHERE id = 1"))

    await alembic("downgrade", "0012")
    await alembic("upgrade", "head")
    await container.database.engine.dispose()

    assert await copy_of(container, counted) == (7, 3, COUNTED_AT)
    assert await copy_of(container, overridden) == (2, 15, OVERRIDDEN_AT)


@pytest.mark.usefixtures("cleanup")
async def test_a_backfilled_copy_accepts_a_newer_event_and_ignores_an_older_one(
    container: Container, catalog_rows: CatalogRows
) -> None:
    counted, *_ = await seed(container, catalog_rows)
    await alembic("downgrade", "0012")
    await alembic("upgrade", "head")
    await container.database.engine.dispose()
    copy = SqlStockCopy(container.database)
    runner = SqlTransactionRunner(container.database)

    async def apply(available: int, as_of: datetime) -> None:
        async def work() -> Ok[None]:
            await copy.apply(counted, available, 3, as_of)
            return Ok(None)

        await runner.run(work)

    await apply(1, datetime(2026, 8, 1, tzinfo=UTC))  # older than the backfilled `updated_at`
    assert await copy_of(container, counted) == (7, 3, COUNTED_AT)

    await apply(4, datetime(2026, 10, 1, tzinfo=UTC))
    assert await copy_of(container, counted) == (4, 3, datetime(2026, 10, 1, tzinfo=UTC))
