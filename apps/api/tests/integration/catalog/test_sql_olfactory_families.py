import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.create_olfactory_family import (
    CreateOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.olfactory_family_status import (
    ArchiveOlfactoryFamily,
    RestoreOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.rename_olfactory_family import (
    RenameOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.queries.list_olfactory_families import (
    ListAdminOlfactoryFamilies,
    ListPublicOlfactoryFamilies,
)
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName, OlfactoryFamily
from fragancia_api.modules.catalog.infrastructure.sql_olfactory_family_repository import (
    SqlOlfactoryFamilyRepository,
)
from fragancia_api.modules.catalog.infrastructure.tables import olfactory_families
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, new_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)

SEEDED = [
    "Acuática",
    "Amaderada",
    "Aromática",
    "Chipre",
    "Cítrica",
    "Floral",
    "Fougère",
    "Gourmand",
    "Oriental",
]


@pytest.fixture
async def empty_families(container: Container) -> AsyncIterator[None]:
    """Empties the table for the test and puts the migration's seed rows back afterwards."""
    async with container.database.engine.begin() as connection:
        saved = (await connection.execute(select(olfactory_families))).mappings().all()
        await connection.execute(text("TRUNCATE catalog.olfactory_families"))
    yield
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.olfactory_families"))
        if saved:
            await connection.execute(insert(olfactory_families), [dict(row) for row in saved])


def _row(name: str, *, active: bool = True) -> dict[str, Any]:
    return {
        "id": new_id(),
        "name": name,
        "slug": slugify(name),
        "is_active": active,
        "created_at": NOW,
    }


async def _create(container: Container, name: str) -> UUID:
    result = await container.services.get(CreateOlfactoryFamily).execute(name)
    assert isinstance(result, Ok)
    return result.value


async def _stored(container: Container, family_id: UUID) -> dict[str, Any]:
    async with container.database.reader() as session:
        stmt = select(olfactory_families).where(olfactory_families.c.id == family_id)
        return dict((await session.execute(stmt)).mappings().one())


async def test_migration_seeds_the_nine_starting_families(container: Container) -> None:
    async with container.database.reader() as session:
        rows = (await session.execute(select(olfactory_families))).mappings().all()
    public = await container.services.get(ListPublicOlfactoryFamilies).execute()

    assert sorted(row["name"] for row in rows) == sorted(SEEDED)
    assert all(row["is_active"] and row["slug"] == slugify(row["name"]) for row in rows)
    assert [f.name for f in public] == SEEDED  # ordered by name, case-insensitive


@pytest.mark.usefixtures("empty_families")
async def test_create_persists_a_trimmed_active_family(container: Container) -> None:
    family_id = await _create(container, "  Especiada ")

    row = await _stored(container, family_id)

    assert (row["name"], row["slug"], row["is_active"]) == ("Especiada", "especiada", True)


@pytest.mark.usefixtures("empty_families")
async def test_the_slug_is_unique_in_the_table(container: Container) -> None:
    from sqlalchemy.exc import IntegrityError

    async with container.database.engine.begin() as connection:
        await connection.execute(insert(olfactory_families).values(_row("Floral")))
    with pytest.raises(IntegrityError, match="uq_olfactory_families_slug"):
        async with container.database.engine.begin() as connection:
            await connection.execute(insert(olfactory_families).values(_row("FLORAL")))


@pytest.mark.usefixtures("empty_families")
async def test_duplicate_family_is_a_conflict_including_archived_ones(
    container: Container,
) -> None:
    first = await _create(container, "Chipre")
    await container.services.get(ArchiveOlfactoryFamily).execute(first)

    result = await container.services.get(CreateOlfactoryFamily).execute("chipre")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_FAMILY_ALREADY_EXISTS"


@pytest.mark.usefixtures("empty_families")
async def test_concurrent_creation_of_the_same_family_yields_one_conflict(
    container: Container,
) -> None:
    create = container.services.get(CreateOlfactoryFamily)

    results = await asyncio.gather(*(create.execute("Floral") for _ in range(5)))

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_FAMILY_ALREADY_EXISTS"
    ] * 4


@pytest.mark.usefixtures("empty_families")
async def test_exists_with_slug_can_ignore_one_row(container: Container) -> None:
    family_id = await _create(container, "Floral")
    repository = SqlOlfactoryFamilyRepository(container.database)
    outcome: list[bool] = []

    async def work() -> Ok[None]:
        outcome.append(await repository.exists_with_slug("floral"))
        outcome.append(await repository.exists_with_slug("floral", except_id=family_id))
        outcome.append(await repository.exists_with_slug("floral", except_id=UUID(int=1)))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert outcome == [True, False, True]


@pytest.mark.usefixtures("empty_families")
async def test_rename_persists_name_and_slug_and_allows_the_same_slug(
    container: Container,
) -> None:
    family_id = await _create(container, "Especiada")
    rename = container.services.get(RenameOlfactoryFamily)

    same_slug = await rename.execute(family_id, "ESPECIADA")
    renamed = await rename.execute(family_id, "Especiada Cálida")

    assert isinstance(same_slug, Ok) and isinstance(renamed, Ok)
    row = await _stored(container, family_id)
    assert (row["name"], row["slug"]) == ("Especiada Cálida", "especiada-calida")


@pytest.mark.usefixtures("empty_families")
async def test_rename_error_cases(container: Container) -> None:
    floral = await _create(container, "Floral")
    await _create(container, "Chipre")
    rename = container.services.get(RenameOlfactoryFamily)

    taken = await rename.execute(floral, "chipre")
    unknown = await rename.execute(UUID(int=404), "Floral")

    assert isinstance(taken, Err) and taken.error.code == "CATALOG_FAMILY_ALREADY_EXISTS"
    assert isinstance(unknown, Err) and unknown.error.code == "CATALOG_FAMILY_NOT_FOUND"
    assert (await _stored(container, floral))["name"] == "Floral"


@pytest.mark.usefixtures("empty_families")
async def test_save_maps_a_slug_clash_to_err_and_keeps_the_transaction_usable(
    container: Container,
) -> None:
    floral = await _create(container, "Floral")
    await _create(container, "Chipre")
    repository = SqlOlfactoryFamilyRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        family = await repository.get_for_update(floral)
        assert family is not None
        family.rename(FamilyName("CHIPRE"))  # skips the exists check, hits the unique index
        outcome.append(await repository.save(family))
        outcome.append(await repository.exists_with_slug("floral"))  # tx still usable
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert isinstance(outcome[0], Err)
    assert outcome[0].error.code == "CATALOG_FAMILY_ALREADY_EXISTS"
    assert outcome[1] is True


@pytest.mark.usefixtures("empty_families")
async def test_concurrent_renames_to_the_same_name_yield_one_success_and_one_conflict(
    container: Container,
) -> None:
    first = await _create(container, "Alpha")
    second = await _create(container, "Beta")
    rename = container.services.get(RenameOlfactoryFamily)

    results = await asyncio.gather(rename.execute(first, "Gamma"), rename.execute(second, "Gamma"))

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_FAMILY_ALREADY_EXISTS"
    ]


@pytest.mark.usefixtures("empty_families")
async def test_archive_and_restore_change_visibility_in_the_queries(container: Container) -> None:
    family_id = await _create(container, "Floral")
    archive = container.services.get(ArchiveOlfactoryFamily)
    restore = container.services.get(RestoreOlfactoryFamily)

    assert isinstance(await archive.execute(family_id), Ok)
    assert isinstance(await archive.execute(family_id), Ok)  # idempotent
    public_archived = await container.services.get(ListPublicOlfactoryFamilies).execute()
    admin = await container.services.get(ListAdminOlfactoryFamilies).execute(page=1, size=10)
    assert isinstance(await restore.execute(family_id), Ok)
    public_restored = await container.services.get(ListPublicOlfactoryFamilies).execute()

    assert public_archived == []
    assert [(f.name, f.is_active) for f in admin.items] == [("Floral", False)]
    assert [f.name for f in public_restored] == ["Floral"]


@pytest.mark.usefixtures("empty_families")
async def test_archive_and_restore_of_an_unknown_family_are_not_found(
    container: Container,
) -> None:
    for command in (
        container.services.get(ArchiveOlfactoryFamily),
        container.services.get(RestoreOlfactoryFamily),
    ):
        result = await command.execute(UUID(int=404))
        assert isinstance(result, Err)
        assert result.error.code == "CATALOG_FAMILY_NOT_FOUND"


@pytest.mark.usefixtures("empty_families")
async def test_queries_order_by_name_filter_active_and_paginate(container: Container) -> None:
    rows = [_row("Zeta"), _row("armada"), _row("Chipre", active=False)]
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(olfactory_families), rows)

    public = await container.services.get(ListPublicOlfactoryFamilies).execute()
    first = await container.services.get(ListAdminOlfactoryFamilies).execute(page=1, size=2)
    last = await container.services.get(ListAdminOlfactoryFamilies).execute(page=2, size=2)

    assert [f.name for f in public] == ["armada", "Zeta"]
    assert [f.name for f in first.items] == ["armada", "Chipre"]
    assert (first.total, last.total) == (3, 3)
    assert [(f.name, f.is_active) for f in last.items] == [("Zeta", True)]


@pytest.mark.usefixtures("empty_families")
async def test_get_for_update_maps_the_row_back_to_a_family(container: Container) -> None:
    family = OlfactoryFamily.create(FamilyName("Gourmand"), created_at=NOW)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(olfactory_families).values(
                id=family.id, name="Gourmand", slug="gourmand", is_active=True, created_at=NOW
            )
        )
    repository = SqlOlfactoryFamilyRepository(container.database)
    loaded: list[OlfactoryFamily | None] = []

    async def work() -> Ok[None]:
        loaded.append(await repository.get_for_update(family.id))
        loaded.append(await repository.get_for_update(UUID(int=404)))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    got = loaded[0]
    assert got is not None
    assert (got.id, got.name.value, got.is_active, got.created_at) == (
        family.id,
        "Gourmand",
        True,
        NOW,
    )
    assert loaded[1] is None
