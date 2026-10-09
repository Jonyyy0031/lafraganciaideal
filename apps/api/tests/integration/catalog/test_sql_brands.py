import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.infrastructure.tables import brands
from fragancia_api.shared.infrastructure.outbox import outbox
from fragancia_api.shared.kernel import Err, Ok, new_id

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def clean_brands(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.brands CASCADE"))


async def test_create_brand_persists_the_row_and_the_event_together(container: Container) -> None:
    result = await container.services.get(CreateBrand).execute("  Maison   Margiela ")

    assert isinstance(result, Ok)
    async with container.database.reader() as session:
        row = (await session.execute(select(brands))).mappings().one()
        event = (await session.execute(select(outbox))).mappings().one()
    assert (row["id"], row["name"], row["slug"], row["is_active"]) == (
        result.value,
        "Maison Margiela",
        "maison-margiela",
        True,
    )
    assert event["name"] == "catalog.brand.created"
    assert event["payload"] == {
        "brand_id": str(result.value),
        "brand_name": "Maison Margiela",
        "slug": "maison-margiela",
    }


async def test_duplicate_slug_is_a_conflict_and_writes_nothing(container: Container) -> None:
    create = container.services.get(CreateBrand)
    await create.execute("Maison Margiela")

    result = await create.execute("maison margiéla")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"
    async with container.database.reader() as session:
        assert len((await session.execute(select(brands))).all()) == 1
        assert len((await session.execute(select(outbox))).all()) == 1


async def test_concurrent_creation_of_the_same_brand_yields_one_conflict(
    container: Container,
) -> None:
    create = container.services.get(CreateBrand)

    results = await asyncio.gather(*(create.execute("Chanel") for _ in range(5)))

    assert sum(isinstance(r, Ok) for r in results) == 1
    conflicts = [r.error.code for r in results if isinstance(r, Err)]
    assert conflicts == ["CATALOG_BRAND_ALREADY_EXISTS"] * 4
    async with container.database.reader() as session:
        assert len((await session.execute(select(brands))).all()) == 1
        assert len((await session.execute(select(outbox))).all()) == 1


async def _insert(name: str, slug: str, *, active: bool = True) -> dict[str, object]:
    return {
        "id": new_id(),
        "name": name,
        "slug": slug,
        "is_active": active,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


async def test_queries_order_by_name_and_filter_active(container: Container) -> None:
    rows = [
        await _insert("Zara", "zara"),
        await _insert("armani", "armani"),
        await _insert("Lancôme", "lancome", active=False),
    ]
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(brands), rows)

    public = await container.services.get(ListPublicBrands).execute()
    admin = await container.services.get(ListAdminBrands).execute(page=1, size=2)
    last = await container.services.get(ListAdminBrands).execute(page=2, size=2)

    assert [b.name for b in public] == ["armani", "Zara"]
    assert [b.name for b in admin.items] == ["armani", "Lancôme"]
    assert (admin.total, last.total) == (3, 3)
    assert [(b.name, b.is_active) for b in last.items] == [("Zara", True)]
    assert admin.items[1].is_active is False


async def test_unique_violation_is_an_err_and_keeps_the_transaction_usable(
    container: Container,
) -> None:
    from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
    from fragancia_api.modules.catalog.infrastructure.sql_brand_repository import (
        SqlBrandRepository,
    )
    from fragancia_api.shared.infrastructure.database import SqlTransactionRunner

    repository = SqlBrandRepository(container.database)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = Brand.create(BrandName("Chanel"), created_at=now)
    clash = Brand.create(BrandName("CHANEL"), created_at=now)  # same slug, skips exists check
    outcome: list[object] = []

    async def work() -> Ok[None]:
        outcome.append(await repository.add(first))
        outcome.append(await repository.add(clash))  # hits uq_brands_slug inside a savepoint
        outcome.append(await repository.exists_with_slug("chanel"))  # tx still usable
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert isinstance(outcome[0], Ok)
    assert isinstance(outcome[1], Err) and outcome[1].error.code == "CATALOG_BRAND_ALREADY_EXISTS"
    assert outcome[2] is True
    async with container.database.reader() as session:
        assert (await session.execute(select(brands.c.id))).scalar_one() == first.id
