"""Integration tests run against `fragancia_test` (migrated by `just test-integration`)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings().for_tests()


@pytest.fixture(scope="session")
async def container(settings: Settings) -> AsyncIterator[Container]:
    container = build_container(settings)
    yield container
    await container.close()


@pytest.fixture(autouse=True)
async def clean_outbox(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE platform.outbox"))
