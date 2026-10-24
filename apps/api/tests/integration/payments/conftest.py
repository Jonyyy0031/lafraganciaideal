from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from fragancia_api.container import Container

# The scratch-database fixture of the migration tests (shared with orders').
from tests.integration.orders.test_migration_0017_delivery_settings import (  # noqa: F401
    scratch,
)


@pytest.fixture(autouse=True)
async def clean_payments(container: Container) -> AsyncIterator[None]:
    """No checkouts and no payments before and after each test."""

    async def reset() -> None:
        async with container.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE payments.checkouts, payments.payments"))

    await reset()
    yield
    await reset()
