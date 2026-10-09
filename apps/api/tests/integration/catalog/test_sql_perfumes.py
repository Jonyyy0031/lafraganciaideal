import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import IntegrityError

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.perfumes import (
    ArchivePerfume,
    CreatePerfume,
    HidePerfume,
    PublishPerfume,
    RestorePerfume,
    UpdatePerfume,
)
from fragancia_api.modules.catalog.application.commands.presentations import (
    AddPresentation,
    ArchivePresentation,
    RestorePresentation,
    UpdatePresentation,
)
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetAdminPerfume,
    ListAdminPerfumes,
)
from fragancia_api.modules.catalog.contracts import PerfumeRequest, PresentationRequest
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    Perfume,
    PerfumeName,
    Price,
    Sale,
)
from fragancia_api.modules.catalog.infrastructure.sql_brand_repository import SqlBrandRepository
from fragancia_api.modules.catalog.infrastructure.sql_concentration_repository import (
    SqlConcentrationRepository,
)
from fragancia_api.modules.catalog.infrastructure.sql_olfactory_family_repository import (
    SqlOlfactoryFamilyRepository,
)
from fragancia_api.modules.catalog.infrastructure.sql_perfume_repository import (
    SqlPerfumeRepository,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Err, Money, Ok, new_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FUTURE = datetime(2030, 1, 1, tzinfo=UTC)
UNKNOWN_ID = UUID(int=404)


@dataclass(frozen=True)
class Refs:
    """Rows this module's tests own: two brands, two families, two concentrations."""

    brand: UUID
    other_brand: UUID
    family: UUID
    other_family: UUID
    concentration: UUID
    other_concentration: UUID


# The brand names sort "Zz Alpha" < "zz beta" only when compared case-insensitively.
BRAND, OTHER_BRAND = "Zz Alpha", "zz beta"


@pytest.fixture
async def refs(container: Container) -> AsyncIterator[Refs]:
    """Owns its brands, families and concentrations (the seeds are left alone) and empties the
    perfume tables before and after each test."""
    values = Refs(*(new_id() for _ in range(6)))
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(
            insert(brands),
            [
                _named(values.brand, BRAND),
                _named(values.other_brand, OTHER_BRAND),
            ],
        )
        await connection.execute(
            insert(olfactory_families),
            [
                _named(values.family, "Zz Family A"),
                _named(values.other_family, "Zz Family B", active=False),
            ],
        )
        await connection.execute(
            insert(concentrations),
            [
                _concentration(values.concentration, "Zz Concentration A", "ZA"),
                _concentration(values.other_concentration, "Zz Concentration B", "ZB"),
            ],
        )
    yield values
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(
            delete(brands).where(brands.c.id.in_([values.brand, values.other_brand]))
        )
        await connection.execute(
            delete(olfactory_families).where(
                olfactory_families.c.id.in_([values.family, values.other_family])
            )
        )
        await connection.execute(
            delete(concentrations).where(
                concentrations.c.id.in_([values.concentration, values.other_concentration])
            )
        )


def _named(row_id: UUID, name: str, *, active: bool = True) -> dict[str, Any]:
    return {
        "id": row_id,
        "name": name,
        "slug": slugify(name),
        "is_active": active,
        "created_at": NOW,
    }


def _concentration(row_id: UUID, name: str, abbreviation: str) -> dict[str, Any]:
    return {
        **_named(row_id, name),
        "abbreviation": abbreviation,
        "abbreviation_slug": slugify(abbreviation),
    }


def _request(refs: Refs, **overrides: Any) -> PerfumeRequest:
    values: dict[str, Any] = {
        "brand_id": refs.brand,
        "concentration_id": refs.concentration,
        "family_id": refs.family,
        "name": "Eros",
        "gender": "men",
    }
    values.update(overrides)
    return PerfumeRequest.model_validate(values)


def _presentation(**overrides: Any) -> PresentationRequest:
    values: dict[str, Any] = {"ml": 100, "price_cents": 250_000, "availability": "in_stock"}
    values.update(overrides)
    return PresentationRequest.model_validate(values)


async def _create(container: Container, refs: Refs, **overrides: Any) -> UUID:
    result = await container.services.get(CreatePerfume).execute(_request(refs, **overrides))
    assert isinstance(result, Ok), result
    return result.value


async def _add(container: Container, perfume_id: UUID, **overrides: Any) -> UUID:
    result = await container.services.get(AddPresentation).execute(
        perfume_id, _presentation(**overrides)
    )
    assert isinstance(result, Ok), result
    return result.value


async def _perfume_row(container: Container, perfume_id: UUID) -> dict[str, Any]:
    async with container.database.reader() as session:
        statement = select(perfumes).where(perfumes.c.id == perfume_id)
        return dict((await session.execute(statement)).mappings().one())


async def _presentation_rows(container: Container, perfume_id: UUID) -> list[dict[str, Any]]:
    async with container.database.reader() as session:
        statement = (
            select(presentations)
            .where(presentations.c.perfume_id == perfume_id)
            .order_by(presentations.c.ml)
        )
        return [dict(row) for row in (await session.execute(statement)).mappings().all()]


def _aggregate(
    refs: Refs, name: str = "Eros", *, concentration: UUID | None = None, slug: str | None = None
) -> Perfume:
    perfume_name = PerfumeName(name)
    return Perfume.create(
        brand_id=refs.brand,
        concentration_id=concentration or refs.concentration,
        family_id=refs.family,
        name=perfume_name,
        slug=slug or slugify(f"{BRAND} {name} {concentration or refs.concentration}"),
        gender=Gender.MEN,
        description=Description(""),
        notes=Notes((), (), ()),
        now=NOW,
    )


def _perfume_values(refs: Refs, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": new_id(),
        "brand_id": refs.brand,
        "concentration_id": refs.concentration,
        "family_id": refs.family,
        "name": "Eros",
        "name_slug": "eros",
        "slug": f"slug-{new_id()}",
        "gender": "men",
        "description": "",
        "top_notes": [],
        "heart_notes": [],
        "base_notes": [],
        "is_published": False,
        "is_archived": False,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


def _presentation_values(perfume_id: UUID, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": new_id(),
        "perfume_id": perfume_id,
        "ml": 100,
        "price_cents": 250_000,
        "availability": "in_stock",
        "is_active": True,
        "created_at": NOW,
    }
    values.update(overrides)
    return values


async def _in_transaction(container: Container, work: Any) -> None:
    await SqlTransactionRunner(container.database).run(work)


# --- tables and constraints -------------------------------------------------------------------


async def test_identity_is_unique_per_brand_name_slug_and_concentration(
    container: Container, refs: Refs
) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(perfumes).values(_perfume_values(refs)))
        # another brand, another concentration and another name slug are all fine
        await connection.execute(
            insert(perfumes).values(_perfume_values(refs, brand_id=refs.other_brand))
        )
        await connection.execute(
            insert(perfumes).values(
                _perfume_values(refs, concentration_id=refs.other_concentration)
            )
        )
        await connection.execute(insert(perfumes).values(_perfume_values(refs, name_slug="other")))

    with pytest.raises(IntegrityError, match="uq_perfumes_identity"):
        async with container.database.engine.begin() as connection:
            await connection.execute(insert(perfumes).values(_perfume_values(refs)))


async def test_the_stored_slug_is_unique(container: Container, refs: Refs) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(perfumes).values(_perfume_values(refs, slug="same-slug")))

    with pytest.raises(IntegrityError, match="uq_perfumes_slug"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(perfumes).values(_perfume_values(refs, slug="same-slug", name_slug="x"))
            )


async def test_ml_is_unique_within_a_perfume_but_not_across_perfumes(
    container: Container, refs: Refs
) -> None:
    first = _perfume_values(refs)
    second = _perfume_values(refs, name_slug="second")
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(perfumes).values([first, second]))
        await connection.execute(insert(presentations).values(_presentation_values(first["id"])))
        await connection.execute(insert(presentations).values(_presentation_values(second["id"])))

    with pytest.raises(IntegrityError, match="uq_presentations_perfume_ml"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(presentations).values(_presentation_values(first["id"], price_cents=1))
            )


@pytest.mark.parametrize("column", ["brand_id", "concentration_id", "family_id"])
async def test_a_perfume_needs_existing_references(
    container: Container, refs: Refs, column: str
) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(perfumes).values(_perfume_values(refs, **{column: UNKNOWN_ID}))
            )


async def test_a_presentation_needs_an_existing_perfume(container: Container, refs: Refs) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        async with container.database.engine.begin() as connection:
            await connection.execute(insert(presentations).values(_presentation_values(UNKNOWN_ID)))


async def test_a_referenced_brand_cannot_be_deleted_and_truncate_cascades_to_the_children(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    await _add(container, perfume_id)

    with pytest.raises(IntegrityError, match="foreign key"):
        async with container.database.engine.begin() as connection:
            await connection.execute(delete(brands).where(brands.c.id == refs.brand))
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
    assert await _presentation_rows(container, perfume_id) == []


# --- create and the mappers -------------------------------------------------------------------


async def test_create_persists_the_row_with_its_slug_notes_and_defaults(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(
        container,
        refs,
        name="  Eros   Flame ",
        gender="unisex",
        description="  Warm ",
        top_notes=[" Pink  Pepper ", "Lemon", "Lemon"],
        heart_notes=["Rose"],
        base_notes=[],
    )

    row = await _perfume_row(container, perfume_id)

    assert row["name"] == "Eros Flame"
    assert row["name_slug"] == "eros-flame"
    assert row["slug"] == "zz-alpha-eros-flame-za"
    assert row["gender"] == "unisex"
    assert row["description"] == "Warm"
    assert row["top_notes"] == ["Pink Pepper", "Lemon", "Lemon"]  # order and duplicates kept
    assert row["heart_notes"] == ["Rose"]
    assert row["base_notes"] == []
    assert (row["brand_id"], row["concentration_id"], row["family_id"]) == (
        refs.brand,
        refs.concentration,
        refs.family,
    )
    assert (row["is_published"], row["is_archived"], row["first_published_at"]) == (
        False,
        False,
        None,
    )
    assert row["created_at"] == row["updated_at"]
    assert await _presentation_rows(container, perfume_id) == []


async def test_a_duplicate_identity_is_a_conflict_including_archived_perfumes(
    container: Container, refs: Refs
) -> None:
    first = await _create(container, refs)
    await container.services.get(ArchivePerfume).execute(first)

    result = await container.services.get(CreatePerfume).execute(_request(refs, name=" EROS "))

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_ALREADY_EXISTS"


async def test_the_same_name_under_another_brand_or_concentration_is_another_perfume(
    container: Container, refs: Refs
) -> None:
    await _create(container, refs)

    other_brand = await _create(container, refs, brand_id=refs.other_brand)
    other_concentration = await _create(container, refs, concentration_id=refs.other_concentration)

    assert (await _perfume_row(container, other_brand))["slug"] == "zz-beta-eros-za"
    assert (await _perfume_row(container, other_concentration))["slug"] == "zz-alpha-eros-zb"


async def test_create_refuses_an_archived_family_and_stores_nothing(
    container: Container, refs: Refs
) -> None:
    result = await container.services.get(CreatePerfume).execute(
        _request(refs, family_id=refs.other_family)
    )

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_FAMILY_UNAVAILABLE"
    async with container.database.reader() as session:
        assert (await session.execute(select(perfumes))).all() == []


async def test_add_maps_each_unique_violation_to_err_and_keeps_the_transaction_usable(
    container: Container, refs: Refs
) -> None:
    await _create(container, refs)  # slug zz-alpha-eros-za, identity (brand, eros, A)
    repository = SqlPerfumeRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        # skips the exists check, so the unique constraints are the ones that answer
        outcome.append(await repository.add(_aggregate(refs, "Eros")))  # the identity
        outcome.append(  # a free identity whose stored slug is taken
            await repository.add(_aggregate(refs, "Other", slug="zz-alpha-eros-za"))
        )
        outcome.append(
            await repository.exists_with_identity(refs.brand, "eros", refs.concentration)
        )
        return Ok(None)

    await _in_transaction(container, work)

    assert isinstance(outcome[0], Err)
    assert outcome[0].error.code == "CATALOG_PERFUME_ALREADY_EXISTS"
    assert isinstance(outcome[1], Err)
    assert outcome[1].error.code == "CATALOG_PERFUME_ALREADY_EXISTS"
    assert outcome[2] is True  # the transaction is still usable


async def test_add_persists_the_presentations_of_the_aggregate(
    container: Container, refs: Refs
) -> None:
    perfume = _aggregate(refs, "With Sizes")
    perfume.add_presentation(
        Ml(50),
        Price(Money(100_000)),
        Sale(Money(90_000), NOW, FUTURE),
        Availability("made_to_order", 7, 10),
        created_at=NOW,
    )
    repository = SqlPerfumeRepository(container.database)

    async def work() -> Ok[None]:
        assert isinstance(await repository.add(perfume), Ok)
        return Ok(None)

    await _in_transaction(container, work)

    [row] = await _presentation_rows(container, perfume.id)
    assert (row["ml"], row["price_cents"], row["sale_price_cents"]) == (50, 100_000, 90_000)
    assert (row["sale_starts_at"], row["sale_ends_at"]) == (NOW, FUTURE)
    assert (row["availability"], row["lead_time_min_days"], row["lead_time_max_days"]) == (
        "made_to_order",
        7,
        10,
    )


async def test_exists_with_identity_can_ignore_one_perfume(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    repository = SqlPerfumeRepository(container.database)
    outcome: list[bool] = []

    async def work() -> Ok[None]:
        for ignored in (None, perfume_id, UNKNOWN_ID):
            outcome.append(
                await repository.exists_with_identity(
                    refs.brand, "eros", refs.concentration, except_id=ignored
                )
            )
        outcome.append(
            await repository.exists_with_identity(refs.brand, "eros", refs.other_concentration)
        )
        outcome.append(
            await repository.exists_with_identity(refs.other_brand, "eros", refs.concentration)
        )
        outcome.append(
            await repository.exists_with_identity(refs.brand, "nope", refs.concentration)
        )
        return Ok(None)

    await _in_transaction(container, work)

    assert outcome == [True, False, True, False, False, False]


async def test_concurrent_creation_of_the_same_perfume_yields_one_conflict(
    container: Container, refs: Refs
) -> None:
    create = container.services.get(CreatePerfume)

    results = await asyncio.gather(*(create.execute(_request(refs)) for _ in range(5)))

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_PERFUME_ALREADY_EXISTS"
    ] * 4
    async with container.database.reader() as session:
        assert len((await session.execute(select(perfumes))).all()) == 1


# --- get_for_update ---------------------------------------------------------------------------


async def test_get_for_update_is_none_for_an_unknown_perfume(
    container: Container, refs: Refs
) -> None:
    repository = SqlPerfumeRepository(container.database)
    loaded: list[Perfume | None] = []

    async def work() -> Ok[None]:
        loaded.append(await repository.get_for_update(UNKNOWN_ID))
        return Ok(None)

    await _in_transaction(container, work)

    assert loaded == [None]


async def test_get_for_update_maps_the_rows_back_and_loads_presentations_by_ml(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(
        container, refs, top_notes=["Mint", "Apple"], heart_notes=["Rose"], description="Warm"
    )
    await _add(container, perfume_id, ml=300)
    await _add(
        container,
        perfume_id,
        ml=100,
        sale_price_cents=200_000,
        sale_starts_at=NOW,
        sale_ends_at=FUTURE,
        availability="made_to_order",
        lead_time_min_days=7,
        lead_time_max_days=10,
    )
    middle = await _add(container, perfume_id, ml=200)
    await container.services.get(ArchivePresentation).execute(perfume_id, middle)
    repository = SqlPerfumeRepository(container.database)
    loaded: list[Perfume | None] = []

    async def work() -> Ok[None]:
        loaded.append(await repository.get_for_update(perfume_id))
        return Ok(None)

    await _in_transaction(container, work)

    [perfume] = loaded
    assert perfume is not None
    assert (perfume.name.value, perfume.slug, perfume.gender) == (
        "Eros",
        "zz-alpha-eros-za",
        Gender.MEN,
    )
    assert perfume.description.value == "Warm"
    assert (perfume.notes.top, perfume.notes.heart, perfume.notes.base) == (
        ("Mint", "Apple"),
        ("Rose",),
        (),
    )
    assert [p.ml.value for p in perfume.presentations] == [100, 200, 300]
    first, second, third = perfume.presentations
    assert first.sale == Sale(Money(200_000), NOW, FUTURE)
    assert first.availability == Availability("made_to_order", 7, 10)
    assert third.sale is None and third.availability == Availability("in_stock", None, None)
    assert (first.is_active, second.is_active, third.is_active) == (True, False, True)
    assert first.price == Price(Money(250_000))


async def test_get_for_update_locks_the_perfume_row_until_the_transaction_ends(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    repository = SqlPerfumeRepository(container.database)
    holding = asyncio.Event()
    release = asyncio.Event()
    waiter_done = asyncio.Event()

    async def holder() -> Ok[None]:
        await repository.get_for_update(perfume_id)
        holding.set()
        await release.wait()
        return Ok(None)

    async def waiter() -> Ok[None]:
        await repository.get_for_update(perfume_id)
        waiter_done.set()
        return Ok(None)

    runner = SqlTransactionRunner(container.database)
    holder_task: asyncio.Task[Any] = asyncio.create_task(runner.run(holder))
    await holding.wait()
    waiter_task: asyncio.Task[Any] = asyncio.create_task(runner.run(waiter))
    await asyncio.sleep(0.5)
    blocked = not waiter_done.is_set()
    release.set()
    await asyncio.gather(holder_task, waiter_task)

    assert blocked
    assert waiter_done.is_set()


# --- save -------------------------------------------------------------------------------------


async def test_save_updates_the_perfume_row_and_keeps_created_at(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    original = await _perfume_row(container, perfume_id)

    result = await container.services.get(UpdatePerfume).execute(
        perfume_id,
        _request(
            refs,
            name="Eros Flame",
            gender="women",
            description="New",
            top_notes=["Lemon"],
            heart_notes=["Rose", "Peony"],
            base_notes=["Musk"],
            brand_id=refs.other_brand,
            concentration_id=refs.other_concentration,
        ),
    )

    assert isinstance(result, Ok)
    row = await _perfume_row(container, perfume_id)
    assert (row["name"], row["name_slug"], row["slug"]) == (
        "Eros Flame",
        "eros-flame",
        "zz-beta-eros-flame-zb",
    )
    assert (row["gender"], row["description"]) == ("women", "New")
    assert (row["top_notes"], row["heart_notes"], row["base_notes"]) == (
        ["Lemon"],
        ["Rose", "Peony"],
        ["Musk"],
    )
    assert (row["brand_id"], row["concentration_id"]) == (
        refs.other_brand,
        refs.other_concentration,
    )
    assert row["created_at"] == original["created_at"]
    assert row["updated_at"] >= original["updated_at"]


async def test_update_keeps_an_unchanged_archived_family_and_changes_to_an_active_one(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.olfactory_families SET is_active = false WHERE id = :id"),
            {"id": refs.family},
        )
    update = container.services.get(UpdatePerfume)

    kept = await update.execute(perfume_id, _request(refs, description="Kept"))
    to_archived = await update.execute(perfume_id, _request(refs, family_id=refs.other_family))
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.olfactory_families SET is_active = true WHERE id = :id"),
            {"id": refs.other_family},
        )
    changed = await update.execute(perfume_id, _request(refs, family_id=refs.other_family))

    assert isinstance(kept, Ok)
    assert isinstance(to_archived, Err)
    assert to_archived.error.code == "CATALOG_PERFUME_FAMILY_UNAVAILABLE"
    assert isinstance(changed, Ok)
    assert (await _perfume_row(container, perfume_id))["family_id"] == refs.other_family


async def test_update_refuses_the_identity_of_another_perfume(
    container: Container, refs: Refs
) -> None:
    await _create(container, refs, name="Eros")
    other = await _create(container, refs, name="Eros Flame")

    result = await container.services.get(UpdatePerfume).execute(other, _request(refs, name="eros"))

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_ALREADY_EXISTS"
    assert (await _perfume_row(container, other))["name"] == "Eros Flame"


async def test_save_upserts_presentations_by_id_without_touching_created_at(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    keep = await _add(container, perfume_id, ml=100)
    change = await _add(container, perfume_id, ml=200)
    before = {row["id"]: row for row in await _presentation_rows(container, perfume_id)}

    updated = await container.services.get(UpdatePresentation).execute(
        perfume_id,
        change,
        _presentation(
            ml=250,
            price_cents=390_000,
            sale_price_cents=350_000,
            sale_starts_at=NOW,
            sale_ends_at=FUTURE,
            availability="made_to_order",
            lead_time_min_days=7,
            lead_time_max_days=10,
        ),
    )
    archived = await container.services.get(ArchivePresentation).execute(perfume_id, change)
    added = await _add(container, perfume_id, ml=50)

    assert isinstance(updated, Ok) and isinstance(archived, Ok)
    rows = {row["id"]: row for row in await _presentation_rows(container, perfume_id)}
    assert len(rows) == 3  # upserts, never duplicate rows
    assert rows[keep] == before[keep]
    assert rows[change]["id"] == change
    assert (rows[change]["ml"], rows[change]["price_cents"], rows[change]["sale_price_cents"]) == (
        250,
        390_000,
        350_000,
    )
    assert (rows[change]["sale_starts_at"], rows[change]["sale_ends_at"]) == (NOW, FUTURE)
    assert (
        rows[change]["availability"],
        rows[change]["lead_time_min_days"],
        rows[change]["lead_time_max_days"],
    ) == ("made_to_order", 7, 10)
    assert rows[change]["is_active"] is False
    assert rows[change]["created_at"] == before[change]["created_at"]
    assert rows[added]["ml"] == 50


async def test_an_update_can_remove_a_sale_and_a_lead_time(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    presentation_id = await _add(
        container,
        perfume_id,
        sale_price_cents=100,
        availability="made_to_order",
        lead_time_min_days=2,
        lead_time_max_days=3,
    )

    result = await container.services.get(UpdatePresentation).execute(
        perfume_id, presentation_id, _presentation()
    )

    assert isinstance(result, Ok)
    [row] = await _presentation_rows(container, perfume_id)
    assert (row["sale_price_cents"], row["sale_starts_at"], row["sale_ends_at"]) == (
        None,
        None,
        None,
    )
    assert (row["availability"], row["lead_time_min_days"], row["lead_time_max_days"]) == (
        "in_stock",
        None,
        None,
    )


async def test_moving_a_presentation_to_the_ml_of_another_is_a_conflict(
    container: Container, refs: Refs
) -> None:
    """Moving a presentation to an ml another one has maps to Err (the unique constraint)."""
    perfume_id = await _create(container, refs)
    await _add(container, perfume_id, ml=100)
    second = await _add(container, perfume_id, ml=200)

    result = await container.services.get(UpdatePresentation).execute(
        perfume_id, second, _presentation(ml=100)
    )

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert [row["ml"] for row in await _presentation_rows(container, perfume_id)] == [100, 200]


async def test_save_maps_a_ml_clash_to_err_rolls_the_savepoint_back_and_keeps_the_transaction(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs, description="Before")
    await _add(container, perfume_id, ml=100)
    await _add(container, perfume_id, ml=200)
    repository = SqlPerfumeRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        perfume = await repository.get_for_update(perfume_id)
        assert perfume is not None
        # skips the aggregate's own check, so the unique constraint is the one that answers
        perfume.description = Description("After")
        perfume.presentations[1].ml = Ml(100)
        outcome.append(await repository.save(perfume))
        outcome.append(
            await repository.exists_with_identity(refs.brand, "eros", refs.concentration)
        )
        return Ok(None)

    await _in_transaction(container, work)

    assert isinstance(outcome[0], Err)
    assert outcome[0].error.code == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert outcome[1] is True  # the transaction is still usable
    assert (await _perfume_row(container, perfume_id))["description"] == "Before"  # rolled back
    assert [row["ml"] for row in await _presentation_rows(container, perfume_id)] == [100, 200]


async def test_save_maps_a_perfume_constraint_clash_to_err(
    container: Container, refs: Refs
) -> None:
    await _create(container, refs, name="Eros")
    other = await _create(container, refs, name="Eros Flame")
    repository = SqlPerfumeRepository(container.database)
    outcome: list[object] = []

    async def work() -> Ok[None]:
        perfume = await repository.get_for_update(other)
        assert perfume is not None
        perfume.name = PerfumeName("Eros")  # same identity as the first, skipping the use case
        outcome.append(await repository.save(perfume))
        perfume = await repository.get_for_update(other)
        assert perfume is not None
        perfume.slug = "zz-alpha-eros-za"  # the slug of the first
        perfume.name = PerfumeName("Free Name")
        outcome.append(await repository.save(perfume))
        return Ok(None)

    await _in_transaction(container, work)

    assert all(isinstance(o, Err) for o in outcome)
    assert [o.error.code for o in outcome if isinstance(o, Err)] == [
        "CATALOG_PERFUME_ALREADY_EXISTS"
    ] * 2


async def test_status_commands_persist_their_flags_and_the_first_publication(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    await _add(container, perfume_id)
    services = container.services

    published = await services.get(PublishPerfume).execute(perfume_id)
    row = await _perfume_row(container, perfume_id)
    first = row["first_published_at"]
    await services.get(HidePerfume).execute(perfume_id)
    hidden = await _perfume_row(container, perfume_id)
    await services.get(PublishPerfume).execute(perfume_id)
    republished = await _perfume_row(container, perfume_id)
    await services.get(ArchivePerfume).execute(perfume_id)
    archived = await _perfume_row(container, perfume_id)
    await services.get(RestorePerfume).execute(perfume_id)
    restored = await _perfume_row(container, perfume_id)

    assert isinstance(published, Ok)
    assert (row["is_published"], first is not None and first.tzinfo is not None) == (True, True)
    assert (hidden["is_published"], hidden["first_published_at"]) == (False, first)
    assert (republished["is_published"], republished["first_published_at"]) == (True, first)
    assert (archived["is_archived"], archived["is_published"]) == (True, False)
    assert (restored["is_archived"], restored["is_published"]) == (False, False)
    assert restored["first_published_at"] == first


async def test_publish_without_an_active_presentation_writes_nothing(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    presentation_id = await _add(container, perfume_id)
    await container.services.get(ArchivePresentation).execute(perfume_id, presentation_id)

    result = await container.services.get(PublishPerfume).execute(perfume_id)
    restored = await container.services.get(RestorePresentation).execute(
        perfume_id, presentation_id
    )
    republished = await container.services.get(PublishPerfume).execute(perfume_id)

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_NOTHING_TO_SELL"
    assert isinstance(restored, Ok) and isinstance(republished, Ok)


async def test_the_last_active_presentation_of_a_published_perfume_stays_active(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    only = await _add(container, perfume_id)
    await container.services.get(PublishPerfume).execute(perfume_id)

    result = await container.services.get(ArchivePresentation).execute(perfume_id, only)

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_LAST_PRESENTATION"
    [row] = await _presentation_rows(container, perfume_id)
    assert row["is_active"] is True


# --- concurrency ------------------------------------------------------------------------------


async def test_concurrent_adds_of_the_same_ml_yield_one_success_and_conflicts_never_an_error(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    add = container.services.get(AddPresentation)

    results = await asyncio.gather(
        *(add.execute(perfume_id, _presentation(ml=100)) for _ in range(5))
    )

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_PRESENTATION_ALREADY_EXISTS"
    ] * 4
    assert len(await _presentation_rows(container, perfume_id)) == 1


async def test_concurrent_adds_of_different_sizes_all_succeed_and_none_is_lost(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    add = container.services.get(AddPresentation)

    results = await asyncio.gather(
        *(add.execute(perfume_id, _presentation(ml=ml)) for ml in (30, 50, 100, 200, 500))
    )

    assert all(isinstance(r, Ok) for r in results)
    assert [row["ml"] for row in await _presentation_rows(container, perfume_id)] == [
        30,
        50,
        100,
        200,
        500,
    ]


async def test_concurrent_updates_of_the_same_perfume_are_serialized(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(container, refs)
    first = await _add(container, perfume_id, ml=100)
    second = await _add(container, perfume_id, ml=200)
    update = container.services.get(UpdatePresentation)

    results = await asyncio.gather(
        update.execute(perfume_id, first, _presentation(ml=300)),
        update.execute(perfume_id, second, _presentation(ml=300)),
    )

    assert sum(isinstance(r, Ok) for r in results) == 1
    assert [r.error.code for r in results if isinstance(r, Err)] == [
        "CATALOG_PRESENTATION_ALREADY_EXISTS"
    ]


# --- queries ----------------------------------------------------------------------------------


async def test_get_admin_returns_the_detail_with_refs_and_presentations_by_ml(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _create(
        container,
        refs,
        description="Warm",
        top_notes=["Mint"],
        heart_notes=["Rose", "Peony"],
        base_notes=["Musk"],
        family_id=refs.family,
    )
    await _add(container, perfume_id, ml=200, price_cents=390_000)
    await _add(
        container,
        perfume_id,
        ml=100,
        sale_price_cents=200_000,
        sale_ends_at=FUTURE,
        availability="made_to_order",
        lead_time_min_days=7,
        lead_time_max_days=10,
    )
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.concentrations SET is_active = false WHERE id = :id"),
            {"id": refs.concentration},
        )

    result = await container.services.get(GetAdminPerfume).execute(perfume_id)

    assert isinstance(result, Ok)
    detail = result.value
    assert (detail.id, detail.slug, detail.name, detail.gender) == (
        perfume_id,
        "zz-alpha-eros-za",
        "Eros",
        "men",
    )
    assert (detail.description, detail.top_notes, detail.heart_notes, detail.base_notes) == (
        "Warm",
        ["Mint"],
        ["Rose", "Peony"],
        ["Musk"],
    )
    assert (detail.brand.id, detail.brand.name, detail.brand.is_active) == (
        refs.brand,
        BRAND,
        True,
    )
    assert (
        detail.concentration.id,
        detail.concentration.name,
        detail.concentration.abbreviation,
        detail.concentration.is_active,
    ) == (refs.concentration, "Zz Concentration A", "ZA", False)
    assert (detail.family.id, detail.family.name, detail.family.is_active) == (
        refs.family,
        "Zz Family A",
        True,
    )
    assert (detail.is_published, detail.first_published_at, detail.is_archived) == (
        False,
        None,
        False,
    )
    assert [p.ml for p in detail.presentations] == [100, 200]
    small, big = detail.presentations
    assert (small.sale_price_cents, small.sale_ends_at, small.sale_starts_at) == (
        200_000,
        FUTURE,
        None,
    )
    assert (small.availability, small.lead_time_min_days, small.lead_time_max_days) == (
        "made_to_order",
        7,
        10,
    )
    assert (big.price_cents, big.availability, big.is_active) == (390_000, "in_stock", True)


async def test_get_admin_of_an_unknown_perfume_is_not_found(
    container: Container, refs: Refs
) -> None:
    result = await container.services.get(GetAdminPerfume).execute(UNKNOWN_ID)

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_NOT_FOUND"


async def test_get_admin_shows_the_state_after_publishing(container: Container, refs: Refs) -> None:
    perfume_id = await _create(container, refs)
    await _add(container, perfume_id)
    await container.services.get(PublishPerfume).execute(perfume_id)

    result = await container.services.get(GetAdminPerfume).execute(perfume_id)

    assert isinstance(result, Ok)
    assert result.value.is_published is True
    assert result.value.first_published_at is not None


async def test_the_list_orders_by_brand_then_name_case_insensitively_then_id(
    container: Container, refs: Refs
) -> None:
    await _create(container, refs, name="zeta")  # brand "Zz Alpha"
    await _create(container, refs, name="Alpha")
    await _create(container, refs, brand_id=refs.other_brand, name="Bravo")  # brand "zz beta"
    await _create(container, refs, brand_id=refs.other_brand, name="alpha")
    await _create(container, refs, name="Alpha", concentration_id=refs.other_concentration)

    page = await container.services.get(ListAdminPerfumes).execute(page=1, size=50, archived=False)

    assert [(p.brand.name, p.name) for p in page.items] == [
        (BRAND, "Alpha"),
        (BRAND, "Alpha"),
        (BRAND, "zeta"),
        (OTHER_BRAND, "alpha"),
        (OTHER_BRAND, "Bravo"),
    ]
    twins = [p for p in page.items if p.name == "Alpha"]
    assert [p.id for p in twins] == sorted(p.id for p in twins)  # the id breaks the tie
    assert page.total == 5


async def test_the_list_counts_active_presentations_and_shows_the_refs(
    container: Container, refs: Refs
) -> None:
    empty = await _create(container, refs, name="Empty")
    some = await _create(container, refs, name="Some")
    other = await _create(container, refs, name="Other", concentration_id=refs.other_concentration)
    first = await _add(container, some, ml=100)
    await _add(container, some, ml=200)
    await _add(container, some, ml=300)
    await _add(container, other, ml=100)
    await container.services.get(ArchivePresentation).execute(some, first)

    page = await container.services.get(ListAdminPerfumes).execute(page=1, size=50, archived=False)

    by_id = {p.id: p for p in page.items}
    assert by_id[empty].active_presentations == 0
    assert by_id[some].active_presentations == 2  # the archived one is not counted
    assert by_id[other].active_presentations == 1  # nor are other perfumes' presentations
    summary = by_id[some]
    assert (summary.slug, summary.gender, summary.is_published, summary.is_archived) == (
        "zz-alpha-some-za",
        "men",
        False,
        False,
    )
    assert (summary.brand.id, summary.brand.name, summary.brand.is_active) == (
        refs.brand,
        BRAND,
        True,
    )
    assert (summary.concentration.abbreviation, summary.concentration.name) == (
        "ZA",
        "Zz Concentration A",
    )
    assert by_id[other].concentration.abbreviation == "ZB"


async def test_the_list_filters_by_archived_and_counts_each_side(
    container: Container, refs: Refs
) -> None:
    kept = await _create(container, refs, name="Kept")
    gone = await _create(container, refs, name="Gone")
    also_gone = await _create(container, refs, name="Also Gone")
    await container.services.get(ArchivePerfume).execute(gone)
    await container.services.get(ArchivePerfume).execute(also_gone)
    list_admin = container.services.get(ListAdminPerfumes)

    active = await list_admin.execute(page=1, size=50, archived=False)
    archived = await list_admin.execute(page=1, size=50, archived=True)

    assert [p.id for p in active.items] == [kept]
    assert active.total == 1
    assert sorted(p.id for p in archived.items) == sorted([gone, also_gone])
    assert archived.total == 2
    assert all(p.is_archived for p in archived.items)


async def test_the_list_paginates_and_reports_the_total(container: Container, refs: Refs) -> None:
    for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo"):
        await _create(container, refs, name=name)
    list_admin = container.services.get(ListAdminPerfumes)

    first = await list_admin.execute(page=1, size=2, archived=False)
    last = await list_admin.execute(page=3, size=2, archived=False)
    beyond = await list_admin.execute(page=4, size=2, archived=False)

    assert [p.name for p in first.items] == ["Alpha", "Bravo"]
    assert [p.name for p in last.items] == ["Echo"]
    assert beyond.items == []
    assert (first.total, last.total, beyond.total) == (5, 5, 5)
    assert (first.page, first.size) == (1, 2)


async def test_an_empty_list_is_an_empty_page(container: Container, refs: Refs) -> None:
    page = await container.services.get(ListAdminPerfumes).execute(page=1, size=10, archived=False)

    assert (page.items, page.total) == ([], 0)


# --- get on the reference repositories --------------------------------------------------------


async def test_brand_get_returns_the_aggregate_or_none(container: Container, refs: Refs) -> None:
    repository = SqlBrandRepository(container.database)
    loaded: list[Any] = []

    async def work() -> Ok[None]:
        loaded.extend([await repository.get(refs.brand), await repository.get(UNKNOWN_ID)])
        return Ok(None)

    await _in_transaction(container, work)

    brand, unknown = loaded
    assert brand is not None
    assert (brand.id, brand.name.value, brand.is_active, brand.created_at) == (
        refs.brand,
        BRAND,
        True,
        NOW,
    )
    assert unknown is None


async def test_family_get_returns_archived_ones_too_and_none_for_unknown(
    container: Container, refs: Refs
) -> None:
    repository = SqlOlfactoryFamilyRepository(container.database)
    loaded: list[Any] = []

    async def work() -> Ok[None]:
        loaded.extend(
            [
                await repository.get(refs.family),
                await repository.get(refs.other_family),
                await repository.get(UNKNOWN_ID),
            ]
        )
        return Ok(None)

    await _in_transaction(container, work)

    active, archived, unknown = loaded
    assert (active.name.value, active.is_active) == ("Zz Family A", True)
    assert (archived.name.value, archived.is_active) == ("Zz Family B", False)
    assert unknown is None


async def test_concentration_get_returns_the_abbreviation_or_none(
    container: Container, refs: Refs
) -> None:
    repository = SqlConcentrationRepository(container.database)
    loaded: list[Any] = []

    async def work() -> Ok[None]:
        loaded.extend([await repository.get(refs.concentration), await repository.get(UNKNOWN_ID)])
        return Ok(None)

    await _in_transaction(container, work)

    concentration, unknown = loaded
    assert (concentration.name.value, concentration.abbreviation.value) == (
        "Zz Concentration A",
        "ZA",
    )
    assert concentration.is_active is True
    assert unknown is None
