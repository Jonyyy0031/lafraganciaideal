from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from tests.integration.catalog.presentation_rows import catalog_rows  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
async def clean_inventory(container: Container) -> AsyncIterator[None]:
    """Empty ledger and the seeded global threshold (3) before and after each test."""

    async def reset() -> None:
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE inventory.stock_items, inventory.stock_movements, "
                    "inventory.reservations, inventory.reservation_lines"
                )
            )
            # Plan 003 tests leave outbox rows: the shared fixture only empties it before.
            await connection.execute(text("TRUNCATE platform.outbox"))
            await connection.execute(
                text("UPDATE inventory.settings SET low_stock_threshold = 3 WHERE id = 1")
            )

    await reset()
    yield
    await reset()
