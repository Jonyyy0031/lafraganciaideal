from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from tests.integration.catalog.presentation_rows import catalog_rows  # noqa: F401  (fixture)

# The scratch-database fixture of the migration tests, shared by 0017 and 0018.
from tests.integration.orders.test_migration_0017_delivery_settings import (  # noqa: F401
    scratch,
)

# The migration's seeded default row: every method off, prices 0, no codes, empty strings.
_DEFAULT_ROW = text(
    "INSERT INTO orders.delivery_settings (id, home_enabled, home_price_cents, "
    "home_free_from_cents, local_enabled, local_price_cents, local_postal_codes, "
    "pickup_enabled, pickup_address, pickup_hours) "
    "VALUES (1, false, 0, NULL, false, 0, ARRAY[]::varchar(5)[], false, '', '')"
)


@pytest.fixture(autouse=True)
async def clean_orders(container: Container) -> AsyncIterator[None]:
    """The seeded default row, no carts, no orders, the order counter at 1000 and an empty stock
    ledger before and after each test."""

    async def reset() -> None:
        async with container.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE orders.delivery_settings"))
            await connection.execute(_DEFAULT_ROW)
            await connection.execute(text("TRUNCATE orders.carts CASCADE"))
            # Plan 003: no orders, the gapless counter back at its seed, and an empty stock ledger.
            await connection.execute(text("TRUNCATE orders.orders CASCADE"))
            await connection.execute(
                text("UPDATE orders.order_numbers SET last_number = 1000 WHERE id = 1")
            )
            # Plan 005: no lookup attempts counted.
            await connection.execute(text("TRUNCATE orders.lookup_throttle"))
            await connection.execute(
                text(
                    "TRUNCATE inventory.stock_items, inventory.stock_movements, "
                    "inventory.reservations, inventory.reservation_lines"
                )
            )

    await reset()
    yield
    await reset()
