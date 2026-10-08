import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import insert, select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.brand_status import (
    ArchiveBrand,
    RestoreBrand,
)
from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.commands.rename_brand import RenameBrand
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
from fragancia_api.modules.catalog.infrastructure.sql_brand_repository import SqlBrandRepository
from fragancia_api.modules.catalog.infrastructure.tables import brands
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Err, Ok

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def clean_brands(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.brands"))


async def _create(container: Container, name: str) -> UUID:
    result = await container.services.get(CreateBrand).execute(name)
    assert isinstance(result, Ok)
    return result.value


async def _row(container: Container, brand_id: UUID) -> dict[str, object]:
    async with container.database.reader() as session:
        stmt = select(brands).where(brands.c.id == brand_id)
        return dict((await session.execute(stmt)).mappings().one())


async def test_rename_persists_name_and_slug(container: Container) -> None:
    brand_id = await _create(container, "Dior")

    result = await container.services.get(RenameBrand).execute(brand_id, " Christian  Dior ")

    assert isinstance(result, Ok)
    row = await _row(container, brand_id)
    assert (row["name"], row["slug"], row["is_active"]) == (
        "Christian Dior",
        "christian-dior",
        True,
    )


async def test_rename_to_the_same_slug_succeeds_and_updates_the_name(container: Container) -> None:
    brand_id = await _create(container, "Dior")

    result = await container.services.get(RenameBrand).execute(brand_id, "DIOR")

    assert isinstance(result, Ok)
    row = await _row(container, brand_id)
    assert (row["name"], row["slug"]) == ("DIOR", "dior")


async def test_rename_to_another_brands_slug_is_a_conflict_and_changes_nothing(
    container: Container,
) -> None:
    dior = await _create(container, "Dior")
    await _create(container, "Chanel")

    result = await container.services.get(RenameBrand).execute(dior, "chanel")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"
    assert (await _row(container, dior))["name"] == "Dior"


async def test_rename_of_an_unknown_brand_is_not_found(container: Container) -> None:
    result = await container.services.get(RenameBrand).execute(UUID(int=404), "Dior")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_NOT_FOUND"


async def test_exists_with_slug_can_ignore_one_row(container: Container) -> None:
    dior = await _create(container, "Dior")
    repository = SqlBrandRepository(container.database)
    outcome: list[bool] = []

    async def work() -> Ok[None]:
        outcome.append(await repository.exists_with_slug("dior"))
        outcome.append(await repository.exists_with_slug("dior", except_id=dior))
        outcome.append(await repository.exists_with_slug("dior", except_id=UUID(int=1)))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert outcome == [True, False, True]


async def test_save_maps_a_slug_clash_to_err_and_keeps_the_transaction_usable(
    container: Container,
) -> None:
    dior = await _create(container, "Dior")
    await _create(container, "Chanel")
    repository = SqlBrandRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        brand = await repository.get_for_update(dior)
        assert brand is not None
        brand.rename(BrandName("CHANEL"))  # skips the exists check, hits uq_brands_slug
        outcome.append(await repository.save(brand))
        outcome.append(await repository.exists_with_slug("dior"))  # tx still usable
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert isinstance(outcome[0], Err)
    assert outcome[0].error.code == "CATALOG_BRAND_ALREADY_EXISTS"
    assert outcome[1] is True
    assert (await _row(container, dior))["name"] == "Dior"


async def test_concurrent_renames_to_the_same_name_yield_one_success_and_one_conflict(
    container: Container,
) -> None:
    first = await _create(container, "Alpha")
    second = await _create(container, "Beta")
    rename = container.services.get(RenameBrand)

    results = await asyncio.gather(rename.execute(first, "Gamma"), rename.execute(second, "Gamma"))

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == ["CATALOG_BRAND_ALREADY_EXISTS"]
    async with container.database.reader() as session:
        slugs = sorted((await session.execute(select(brands.c.slug))).scalars())
    assert "gamma" in slugs and len(slugs) == 2 and len(set(slugs)) == 2


async def test_archive_hides_a_brand_from_the_public_list_and_restore_brings_it_back(
    container: Container,
) -> None:
    brand_id = await _create(container, "Dior")
    archive = container.services.get(ArchiveBrand)
    restore = container.services.get(RestoreBrand)

    assert isinstance(await archive.execute(brand_id), Ok)
    assert isinstance(await archive.execute(brand_id), Ok)  # idempotent
    public_archived = await container.services.get(ListPublicBrands).execute()
    admin_archived = await container.services.get(ListAdminBrands).execute(page=1, size=10)
    assert isinstance(await restore.execute(brand_id), Ok)
    public_restored = await container.services.get(ListPublicBrands).execute()

    assert public_archived == []
    assert [(b.name, b.is_active) for b in admin_archived.items] == [("Dior", False)]
    assert [b.name for b in public_restored] == ["Dior"]


async def test_an_archived_slug_stays_taken_for_new_brands(container: Container) -> None:
    brand_id = await _create(container, "Dior")
    await container.services.get(ArchiveBrand).execute(brand_id)

    result = await container.services.get(CreateBrand).execute("dior")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"


async def test_archive_and_restore_of_an_unknown_brand_are_not_found(container: Container) -> None:
    for command in (container.services.get(ArchiveBrand), container.services.get(RestoreBrand)):
        result = await command.execute(UUID(int=404))
        assert isinstance(result, Err)
        assert result.error.code == "CATALOG_BRAND_NOT_FOUND"


async def test_get_for_update_maps_the_row_back_to_a_brand(container: Container) -> None:
    brand = Brand.create(BrandName("Maison Margiela"), created_at=NOW)
    brand.is_active = False
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(brands).values(
                id=brand.id,
                name="Maison Margiela",
                slug=brand.slug,
                is_active=False,
                created_at=NOW,
            )
        )
    repository = SqlBrandRepository(container.database)
    loaded: list[Brand | None] = []

    async def work() -> Ok[None]:
        loaded.append(await repository.get_for_update(brand.id))
        loaded.append(await repository.get_for_update(UUID(int=404)))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    got = loaded[0]
    assert got is not None
    assert (got.id, got.name.value, got.is_active, got.created_at) == (
        brand.id,
        "Maison Margiela",
        False,
        NOW,
    )
    assert loaded[1] is None
