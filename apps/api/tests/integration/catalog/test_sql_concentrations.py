import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.concentration_status import (
    ArchiveConcentration,
    RestoreConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_concentration import (
    CreateConcentration,
)
from fragancia_api.modules.catalog.application.commands.update_concentration import (
    UpdateConcentration,
)
from fragancia_api.modules.catalog.application.queries.list_concentrations import (
    ListAdminConcentrations,
    ListPublicConcentrations,
)
from fragancia_api.modules.catalog.domain.concentration import (
    Abbreviation,
    Concentration,
    ConcentrationName,
)
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.infrastructure.sql_concentration_repository import (
    SqlConcentrationRepository,
)
from fragancia_api.modules.catalog.infrastructure.tables import concentrations
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, new_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)

SEEDED = [
    ("Eau de Cologne", "EDC", "eau-de-cologne", "edc"),
    ("Eau de Parfum", "EDP", "eau-de-parfum", "edp"),
    ("Eau de Toilette", "EDT", "eau-de-toilette", "edt"),
    ("Extrait de Parfum", "Extrait", "extrait-de-parfum", "extrait"),
    ("Parfum", "Parfum", "parfum", "parfum"),
]


@pytest.fixture
async def empty_concentrations(container: Container) -> AsyncIterator[None]:
    """Empties the table for the test and puts the migration's seed rows back afterwards."""
    async with container.database.engine.begin() as connection:
        saved = (await connection.execute(select(concentrations))).mappings().all()
        await connection.execute(text("TRUNCATE catalog.concentrations CASCADE"))
    yield
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.concentrations CASCADE"))
        if saved:
            await connection.execute(insert(concentrations), [dict(row) for row in saved])


def _row(name: str, abbreviation: str, *, active: bool = True) -> dict[str, Any]:
    return {
        "id": new_id(),
        "name": name,
        "slug": slugify(name),
        "abbreviation": abbreviation,
        "abbreviation_slug": slugify(abbreviation),
        "is_active": active,
        "created_at": NOW,
    }


def _aggregate(name: str, abbreviation: str) -> Concentration:
    return Concentration(
        id=new_id(),
        name=ConcentrationName(name),
        abbreviation=Abbreviation(abbreviation),
        is_active=True,
        created_at=NOW,
    )


async def _create(container: Container, name: str, abbreviation: str) -> UUID:
    result = await container.services.get(CreateConcentration).execute(name, abbreviation)
    assert isinstance(result, Ok)
    return result.value


async def _stored(container: Container, concentration_id: UUID) -> dict[str, Any]:
    async with container.database.reader() as session:
        stmt = select(concentrations).where(concentrations.c.id == concentration_id)
        return dict((await session.execute(stmt)).mappings().one())


async def test_migration_seeds_the_five_starting_concentrations(container: Container) -> None:
    async with container.database.reader() as session:
        rows = (await session.execute(select(concentrations))).mappings().all()
    public = await container.services.get(ListPublicConcentrations).execute()

    assert sorted(
        (r["name"], r["abbreviation"], r["slug"], r["abbreviation_slug"]) for r in rows
    ) == sorted(SEEDED)
    assert all(row["is_active"] for row in rows)
    assert [(c.name, c.abbreviation, c.slug) for c in public] == [
        (name, abbreviation, slug) for name, abbreviation, slug, _ in SEEDED
    ]  # ordered by name, case-insensitive


@pytest.mark.usefixtures("empty_concentrations")
async def test_create_persists_trimmed_active_texts_and_both_slugs(container: Container) -> None:
    concentration_id = await _create(container, " Body  Mist ", " Mist ")

    row = await _stored(container, concentration_id)

    assert (row["name"], row["slug"], row["abbreviation"], row["abbreviation_slug"]) == (
        "Body Mist",
        "body-mist",
        "Mist",
        "mist",
    )
    assert row["is_active"] is True


@pytest.mark.usefixtures("empty_concentrations")
async def test_both_slugs_are_unique_in_the_table(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(concentrations).values(_row("Floral Mist", "FM")))
    with pytest.raises(IntegrityError, match="uq_concentrations_slug"):
        async with container.database.engine.begin() as connection:
            await connection.execute(insert(concentrations).values(_row("FLORAL MIST", "FX")))
    with pytest.raises(IntegrityError, match="uq_concentrations_abbreviation_slug"):
        async with container.database.engine.begin() as connection:
            await connection.execute(insert(concentrations).values(_row("Other Mist", "fm")))


@pytest.mark.usefixtures("empty_concentrations")
async def test_duplicates_are_conflicts_including_archived_ones(container: Container) -> None:
    first = await _create(container, "Parfum", "Parfum")
    await container.services.get(ArchiveConcentration).execute(first)
    create = container.services.get(CreateConcentration)

    by_name = await create.execute("parfum", "Other")
    by_abbreviation = await create.execute("Other", "PARFUM")

    assert isinstance(by_name, Err)
    assert by_name.error.code == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert isinstance(by_abbreviation, Err)
    assert by_abbreviation.error.code == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"


@pytest.mark.usefixtures("empty_concentrations")
async def test_add_maps_each_unique_violation_to_err_and_keeps_the_transaction_usable(
    container: Container,
) -> None:
    await _create(container, "Floral Mist", "FM")
    repository = SqlConcentrationRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        # skips the exists checks, so the unique indexes are the ones that answer
        outcome.append(await repository.add(_aggregate("Floral Mist", "ZZ")))
        outcome.append(await repository.add(_aggregate("Other Mist", "fm")))
        outcome.append(await repository.exists_with_slug("floral-mist"))  # tx still usable
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert isinstance(outcome[0], Err)
    assert outcome[0].error.code == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert isinstance(outcome[1], Err)
    assert outcome[1].error.code == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    assert outcome[2] is True


@pytest.mark.usefixtures("empty_concentrations")
async def test_concurrent_creation_of_the_same_concentration_yields_one_conflict(
    container: Container,
) -> None:
    create = container.services.get(CreateConcentration)

    results = await asyncio.gather(*(create.execute("Body Mist", f"Mist{n}") for n in range(5)))

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    ] * 4


@pytest.mark.usefixtures("empty_concentrations")
async def test_concurrent_creation_clashing_on_both_texts_yields_one_conflict_never_an_error(
    container: Container,
) -> None:
    """Which unique index PostgreSQL reports first when both clash is its choice (here the
    abbreviation's), so the code is one of the two 409s; the sequential path reports the name."""
    create = container.services.get(CreateConcentration)

    results = await asyncio.gather(*(create.execute("Body Mist", "Mist") for _ in range(5)))

    assert sum(isinstance(r, Ok) for r in results) == 1
    codes = [r.error.code for r in results if isinstance(r, Err)]
    assert len(codes) == 4
    assert set(codes) <= {
        "CATALOG_CONCENTRATION_ALREADY_EXISTS",
        "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN",
    }


@pytest.mark.usefixtures("empty_concentrations")
async def test_exists_checks_can_ignore_one_row(container: Container) -> None:
    concentration_id = await _create(container, "Body Mist", "Mist")
    repository = SqlConcentrationRepository(container.database)
    outcome: list[bool] = []

    async def work() -> Ok[None]:
        for ignored in (None, concentration_id, UUID(int=1)):
            outcome.append(await repository.exists_with_slug("body-mist", except_id=ignored))
            outcome.append(await repository.exists_with_abbreviation("mist", except_id=ignored))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert outcome == [True, True, False, False, True, True]


@pytest.mark.usefixtures("empty_concentrations")
async def test_update_persists_both_texts_and_slugs_and_allows_its_own_slugs(
    container: Container,
) -> None:
    concentration_id = await _create(container, "Body Mist", "Mist")
    update = container.services.get(UpdateConcentration)

    same_slugs = await update.execute(concentration_id, "BODY MIST", "MIST")
    updated = await update.execute(concentration_id, "Body Splash", "Splash")

    assert isinstance(same_slugs, Ok) and isinstance(updated, Ok)
    row = await _stored(container, concentration_id)
    assert (row["name"], row["slug"], row["abbreviation"], row["abbreviation_slug"]) == (
        "Body Splash",
        "body-splash",
        "Splash",
        "splash",
    )


@pytest.mark.usefixtures("empty_concentrations")
async def test_update_error_cases(container: Container) -> None:
    mist = await _create(container, "Body Mist", "Mist")
    await _create(container, "Eau de Parfum", "EDP")
    update = container.services.get(UpdateConcentration)

    name_taken = await update.execute(mist, "eau de parfum", "Mist")
    abbreviation_taken = await update.execute(mist, "Body Mist", "edp")
    unknown = await update.execute(UUID(int=404), "Body Mist", "Mist")

    assert isinstance(name_taken, Err)
    assert name_taken.error.code == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert isinstance(abbreviation_taken, Err)
    assert abbreviation_taken.error.code == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    assert isinstance(unknown, Err)
    assert unknown.error.code == "CATALOG_CONCENTRATION_NOT_FOUND"
    row = await _stored(container, mist)
    assert (row["name"], row["abbreviation"]) == ("Body Mist", "Mist")


@pytest.mark.usefixtures("empty_concentrations")
async def test_save_maps_each_unique_violation_to_err_and_keeps_the_transaction_usable(
    container: Container,
) -> None:
    mist = await _create(container, "Body Mist", "Mist")
    await _create(container, "Eau de Parfum", "EDP")
    repository = SqlConcentrationRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        concentration = await repository.get_for_update(mist)
        assert concentration is not None
        # skips the exists checks, so the unique indexes are the ones that answer
        concentration.update(ConcentrationName("EAU DE PARFUM"), Abbreviation("Mist"))
        outcome.append(await repository.save(concentration))
        concentration.update(ConcentrationName("Body Mist"), Abbreviation("edp"))
        outcome.append(await repository.save(concentration))
        outcome.append(await repository.exists_with_slug("body-mist"))  # tx still usable
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    assert isinstance(outcome[0], Err)
    assert outcome[0].error.code == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert isinstance(outcome[1], Err)
    assert outcome[1].error.code == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    assert outcome[2] is True


@pytest.mark.usefixtures("empty_concentrations")
async def test_concurrent_updates_to_the_same_abbreviation_yield_one_success_and_one_conflict(
    container: Container,
) -> None:
    first = await _create(container, "Alpha", "AL")
    second = await _create(container, "Beta", "BE")
    update = container.services.get(UpdateConcentration)

    results = await asyncio.gather(
        update.execute(first, "Alpha", "Gamma"), update.execute(second, "Beta", "gamma")
    )

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    ]


@pytest.mark.usefixtures("empty_concentrations")
async def test_concurrent_updates_to_the_same_name_yield_one_success_and_one_conflict(
    container: Container,
) -> None:
    first = await _create(container, "Alpha", "AL")
    second = await _create(container, "Beta", "BE")
    update = container.services.get(UpdateConcentration)

    results = await asyncio.gather(
        update.execute(first, "Gamma", "AL"), update.execute(second, "gamma", "BE")
    )

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    ]


@pytest.mark.usefixtures("empty_concentrations")
async def test_archive_and_restore_change_visibility_in_the_queries(container: Container) -> None:
    concentration_id = await _create(container, "Body Mist", "Mist")
    archive = container.services.get(ArchiveConcentration)
    restore = container.services.get(RestoreConcentration)

    assert isinstance(await archive.execute(concentration_id), Ok)
    assert isinstance(await archive.execute(concentration_id), Ok)  # idempotent
    public_archived = await container.services.get(ListPublicConcentrations).execute()
    admin = await container.services.get(ListAdminConcentrations).execute(page=1, size=10)
    assert isinstance(await restore.execute(concentration_id), Ok)
    public_restored = await container.services.get(ListPublicConcentrations).execute()

    assert public_archived == []
    assert [(c.name, c.is_active) for c in admin.items] == [("Body Mist", False)]
    assert [c.name for c in public_restored] == ["Body Mist"]


@pytest.mark.usefixtures("empty_concentrations")
async def test_archive_and_restore_of_an_unknown_concentration_are_not_found(
    container: Container,
) -> None:
    for command in (
        container.services.get(ArchiveConcentration),
        container.services.get(RestoreConcentration),
    ):
        result = await command.execute(UUID(int=404))
        assert isinstance(result, Err)
        assert result.error.code == "CATALOG_CONCENTRATION_NOT_FOUND"


@pytest.mark.usefixtures("empty_concentrations")
async def test_queries_order_by_name_filter_active_and_paginate(container: Container) -> None:
    rows = [_row("Zeta", "ZZ"), _row("armada", "AR"), _row("Chipre", "CH", active=False)]
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(concentrations), rows)

    public = await container.services.get(ListPublicConcentrations).execute()
    first = await container.services.get(ListAdminConcentrations).execute(page=1, size=2)
    last = await container.services.get(ListAdminConcentrations).execute(page=2, size=2)

    assert [(c.name, c.abbreviation, c.slug) for c in public] == [
        ("armada", "AR", "armada"),
        ("Zeta", "ZZ", "zeta"),
    ]
    assert [c.name for c in first.items] == ["armada", "Chipre"]
    assert (first.total, last.total) == (3, 3)
    assert [(c.name, c.is_active) for c in last.items] == [("Zeta", True)]


@pytest.mark.usefixtures("empty_concentrations")
async def test_get_for_update_maps_the_row_back_to_a_concentration(container: Container) -> None:
    concentration = _aggregate("Gourmand Mist", "GM")
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(concentrations).values(
                id=concentration.id,
                name="Gourmand Mist",
                slug="gourmand-mist",
                abbreviation="GM",
                abbreviation_slug="gm",
                is_active=True,
                created_at=NOW,
            )
        )
    repository = SqlConcentrationRepository(container.database)
    loaded: list[Concentration | None] = []

    async def work() -> Ok[None]:
        loaded.append(await repository.get_for_update(concentration.id))
        loaded.append(await repository.get_for_update(UUID(int=404)))
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)

    got = loaded[0]
    assert got is not None
    assert (got.id, got.name.value, got.abbreviation.value, got.is_active, got.created_at) == (
        concentration.id,
        "Gourmand Mist",
        "GM",
        True,
        NOW,
    )
    assert loaded[1] is None
