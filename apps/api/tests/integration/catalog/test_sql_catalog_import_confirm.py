"""Plan 008 against `fragancia_test` (and RustFS): the full confirm and discard through the real
container, the all-or-nothing rollback after writes and savepoint failures in the middle of the
loop, the perfume repository with photos and `source_url`, replacing photos with real objects,
and the lock between concurrent confirms and the expiry job.

Ready imports are built with the real clock (the container's `ConfirmCatalogImport` reads it)."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ConfirmCatalogImport,
    DiscardCatalogImport,
    ExpireCatalogImports,
)
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportPlan,
    ImportStatus,
    ImportSummary,
    PlannedPerfume,
    PlannedPhoto,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import (
    Description,
    Gender,
    Notes,
    Perfume,
    PerfumeName,
    photo_keys,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_media import InMemoryMediaStorage
from fragancia_api.modules.catalog.infrastructure.sql_brand_repository import SqlBrandRepository
from fragancia_api.modules.catalog.infrastructure.sql_catalog_import_repository import (
    SqlCatalogImportRepository,
)
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
    perfume_photos,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database, SqlTransactionRunner
from fragancia_api.shared.infrastructure.in_memory import FixedClock, RecordingEventPublisher
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.catalog.conftest import Bucket
from tests.integration.catalog.import_fixtures import (
    BRAND_NAME,
    CONCENTRATION_ABBREVIATION,
    World,
    seed_perfume,
    seed_photo,
    world,  # noqa: F401  (a fixture)
)

pytestmark = pytest.mark.integration

TTL = timedelta(minutes=60)
ACTOR = "admin-it"
CATALOG_TABLES = (
    "catalog.perfumes",
    "catalog.presentations",
    "catalog.perfume_photos",
    "catalog.perfume_slug_history",
    "catalog.imports",
)


# --- helpers ----------------------------------------------------------------------------------


async def _dump(container: Container) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table the confirm can touch, ordered, as plain tuples."""
    dump: dict[str, list[tuple[Any, ...]]] = {}
    async with container.database.engine.connect() as connection:
        for table in CATALOG_TABLES:
            rows = await connection.execute(text(f"SELECT * FROM {table} ORDER BY 1, 2"))  # noqa: S608
            dump[table] = [tuple(row) for row in rows]
    return dump


async def _presentation_id(container: Container, perfume_id: UUID, ml: int) -> UUID:
    async with container.database.reader() as session:
        found = await session.scalar(
            select(presentations.c.id).where(
                presentations.c.perfume_id == perfume_id, presentations.c.ml == ml
            )
        )
    assert found is not None
    return UUID(str(found))


def _presentation(
    ml: int = 100, price: int = 250_000, *, presentation_id: UUID | None = None, row: int = 2
) -> PlannedPresentation:
    return PlannedPresentation(
        presentation_id, ml, price, None, None, None, "in_stock", None, None, row
    )


def _photo(
    perfume_id: UUID, url: str, position: int = 1, *, replaces: UUID | None = None
) -> PlannedPhoto:
    photo_id = new_id()
    card, detail = photo_keys(perfume_id, photo_id)
    return PlannedPhoto(position, photo_id, url, card, detail, 1200, 900, replaces)


def _planned(
    world: World,  # noqa: F811
    perfume_id: UUID,
    name: str,
    *,
    is_new: bool,
    first_row: int,
    presentations_: tuple[PlannedPresentation, ...],
    photos: tuple[PlannedPhoto, ...] = (),
    publish: bool | None = None,
) -> PlannedPerfume:
    return PlannedPerfume(
        perfume_id=perfume_id,
        is_new=is_new,
        brand_id=world.brand,
        concentration_id=world.concentration,
        family_id=world.family,
        name=name,
        gender=Gender.WOMEN,
        description="Desde el Excel",
        top_notes=("Lima",),
        heart_notes=(),
        base_notes=(),
        publish=publish,
        first_row=first_row,
        presentations=presentations_,
        photos=photos,
    )


def _new_perfume(
    world: World,  # noqa: F811
    name: str,
    row: int,
    *,
    photo_urls: tuple[str, ...] = (),
) -> PlannedPerfume:
    perfume_id = new_id()
    return _planned(
        world,
        perfume_id,
        name,
        is_new=True,
        first_row=row,
        presentations_=(
            _presentation(100, 250_000, row=row),
            _presentation(50, 150_000, row=row + 1),
        ),
        photos=tuple(_photo(perfume_id, url, n) for n, url in enumerate(photo_urls, start=1)),
        publish=True,
    )


async def _store_ready(
    container: Container, *planned: PlannedPerfume, expires_in: timedelta = TTL
) -> UUID:
    now = datetime.now(UTC)
    item = CatalogImport.create(b"PK\x03\x04data", "admin-it", now - timedelta(minutes=5))
    item.start(now - timedelta(minutes=4))
    item.finish_ready(
        now - timedelta(minutes=3),
        expires_in + timedelta(minutes=3),
        len(planned),
        ImportSummary(len(planned), 0, 0, 0, 0),
        ImportPlan(planned),
    )
    runner = SqlTransactionRunner(container.database)
    repository = SqlCatalogImportRepository(container.database)

    async def work() -> Ok[None]:
        await repository.add(item)
        return Ok(None)

    await runner.run(work)
    return item.id


async def _load_import(container: Container, import_id: UUID) -> CatalogImport:
    runner = SqlTransactionRunner(container.database)
    repository = SqlCatalogImportRepository(container.database)
    found: list[CatalogImport] = []

    async def work() -> Ok[None]:
        loaded = await repository.get_for_update(import_id)
        assert loaded is not None
        found.append(loaded)
        return Ok(None)

    await runner.run(work)
    return found[0]


def _issues(result: Result[Any, DomainError]) -> list[tuple[int | None, str]]:
    assert isinstance(result, Err), result
    assert result.error.code == "CATALOG_IMPORT_STALE"
    details = result.error.details
    assert isinstance(details, dict)
    return [(issue["row"], issue["code"]) for issue in details["issues"]]


def _code(result: Result[Any, DomainError]) -> str:
    assert isinstance(result, Err), result
    return result.error.code


class _ProbeRunner(SqlTransactionRunner):
    """The real runner; right after the work (still inside the transaction) it runs `probe`
    on the same session, so a test can see what the confirm had written before the rollback."""

    def __init__(self, container: Container, probe: Callable[[], Awaitable[Any]]) -> None:
        super().__init__(container.database)
        self._probe = probe
        self.seen: Any = None

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        async def probed() -> Result[T, E]:
            result = await work()
            self.seen = await self._probe()
            return result

        return await super().run(probed)


def _confirm_with(
    container: Container,
    runner: SqlTransactionRunner,
    perfumes_repository: SqlPerfumeRepository | None = None,
) -> ConfirmCatalogImport:
    database = container.database
    return ConfirmCatalogImport(
        imports=SqlCatalogImportRepository(database),
        perfumes=perfumes_repository or SqlPerfumeRepository(database),
        brands=SqlBrandRepository(database),
        families=SqlOlfactoryFamilyRepository(database),
        concentrations=SqlConcentrationRepository(database),
        storage=InMemoryMediaStorage(),  # no replaced photo here: nothing to delete
        transactions=runner,
        clock=FixedClock(datetime.now(UTC)),
        events=RecordingEventPublisher(),
    )


# --- the rollback (the must-have) -------------------------------------------------------------


async def test_a_stale_confirm_leaves_every_catalog_table_and_the_import_unchanged(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    updated = await seed_perfume(container, world, "Zz Imp A", presentation_ml=(100,))
    archived = await seed_perfume(
        container, world, "Zz Imp B", archived=True, presentation_ml=(100,)
    )
    await seed_perfume(container, world, "Zz Imp Taken", presentation_ml=(100,))
    updated_presentation = await _presentation_id(container, updated, 100)
    archived_presentation = await _presentation_id(container, archived, 100)
    first_new = _new_perfume(world, "Zz Imp New One", 2, photo_urls=("https://img.test/1.jpg",))
    valid_update = _planned(
        world,
        updated,
        "Zz Imp A",
        is_new=False,
        first_row=4,
        presentations_=(_presentation(100, 111_100, presentation_id=updated_presentation, row=4),),
        publish=True,
    )
    stale_archived = _planned(  # stale in the middle of the plan
        world,
        archived,
        "Zz Imp B",
        is_new=False,
        first_row=5,
        presentations_=(_presentation(100, 222_200, presentation_id=archived_presentation, row=5),),
    )
    # A new perfume whose identity (brand + name + concentration) someone else holds now: the
    # unique violation happens inside the repository's savepoint, mid-loop.
    taken_identity = _new_perfume(world, "Zz Imp Taken", 6, photo_urls=("https://img.test/2.jpg",))
    last_new = _new_perfume(world, "Zz Imp New Two", 8, photo_urls=("https://img.test/3.jpg",))
    import_id = await _store_ready(
        container, first_new, valid_update, stale_archived, taken_identity, last_new
    )
    before = await _dump(container)
    new_ids = [first_new.perfume_id, taken_identity.perfume_id, last_new.perfume_id]

    async def probe() -> dict[str, Any]:
        session = container.database.session
        return {
            "new_perfumes": await session.scalar(
                select(func.count()).select_from(perfumes).where(perfumes.c.id.in_(new_ids))
            ),
            "new_photos": await session.scalar(
                select(func.count())
                .select_from(perfume_photos)
                .where(perfume_photos.c.perfume_id.in_(new_ids))
            ),
            "updated_price": await session.scalar(
                select(presentations.c.price_cents).where(
                    presentations.c.id == updated_presentation
                )
            ),
        }

    runner = _ProbeRunner(container, probe)

    result = await _confirm_with(container, runner).execute(import_id, ACTOR, may_record_stock=True)

    assert _issues(result) == [
        (5, "CATALOG_PERFUME_ARCHIVED"),
        (6, "CATALOG_PERFUME_ALREADY_EXISTS"),
    ]
    # Inside the transaction the valid rows really had been written (so the rollback is what
    # restores the tables below, not an early exit)...
    assert runner.seen == {"new_perfumes": 2, "new_photos": 2, "updated_price": 111_100}
    # ...and afterwards not one row of any catalog table differs, including the import.
    assert await _dump(container) == before
    item = await _load_import(container, import_id)
    assert item.status is ImportStatus.READY
    assert item.plan is not None and len(item.plan.perfumes) == 5
    assert (item.applied_at, item.applied_by) == (None, None)


async def test_after_a_stale_confirm_the_same_import_confirms_once_the_catalog_is_fixed(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    archived = await seed_perfume(
        container, world, "Zz Imp B", archived=True, presentation_ml=(100,)
    )
    presentation_id = await _presentation_id(container, archived, 100)
    new = _new_perfume(world, "Zz Imp New One", 2, photo_urls=("https://img.test/1.jpg",))
    existing = _planned(
        world,
        archived,
        "Zz Imp B",
        is_new=False,
        first_row=4,
        presentations_=(_presentation(100, 333_300, presentation_id=presentation_id, row=4),),
    )
    import_id = await _store_ready(container, new, existing)
    confirm = container.services.get(ConfirmCatalogImport)
    assert _issues(await confirm.execute(import_id, ACTOR, may_record_stock=True)) == [
        (4, "CATALOG_PERFUME_ARCHIVED")
    ]

    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE catalog.perfumes SET is_archived = false WHERE id = :id"),
            {"id": archived},
        )
    result = await confirm.execute(import_id, ACTOR, may_record_stock=True)

    assert isinstance(result, Ok), result
    async with container.database.reader() as session:
        price = await session.scalar(
            select(presentations.c.price_cents).where(presentations.c.id == presentation_id)
        )
        created = await session.scalar(
            select(func.count()).select_from(perfumes).where(perfumes.c.id == new.perfume_id)
        )
    assert (price, created) == (333_300, 1)


async def test_an_exception_in_the_middle_of_the_confirm_rolls_everything_back_too(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    first = _new_perfume(world, "Zz Imp New One", 2)
    second = _new_perfume(world, "Zz Imp New Two", 4)
    import_id = await _store_ready(container, first, second)
    before = await _dump(container)
    calls = 0

    class Exploding(SqlPerfumeRepository):
        async def add(self, perfume: Perfume) -> Result[None, Any]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("connection lost")
            return await super().add(perfume)

    confirm = _confirm_with(
        container, SqlTransactionRunner(container.database), Exploding(container.database)
    )

    with pytest.raises(RuntimeError):
        await confirm.execute(import_id, ACTOR, may_record_stock=True)

    assert calls == 2
    assert await _dump(container) == before


# --- a full confirm ---------------------------------------------------------------------------


async def test_a_valid_confirm_writes_new_and_existing_perfumes_and_applies_the_import(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    existing = await seed_perfume(container, world, "Zz Imp A", presentation_ml=(100,))
    existing_presentation = await _presentation_id(container, existing, 100)
    new = _new_perfume(
        world, "Zz Imp New One", 2, photo_urls=("https://img.test/1.jpg", "https://img.test/2.jpg")
    )
    update = _planned(
        world,
        existing,
        "Zz Imp A",
        is_new=False,
        first_row=4,
        presentations_=(
            _presentation(100, 199_900, presentation_id=existing_presentation, row=4),
            _presentation(30, 90_000, row=5),
        ),
        publish=True,
    )
    import_id = await _store_ready(container, new, update)

    result = await container.services.get(ConfirmCatalogImport).execute(
        import_id, ACTOR, may_record_stock=True
    )

    assert isinstance(result, Ok), result
    async with container.database.reader() as session:
        created = (
            (await session.execute(select(perfumes).where(perfumes.c.id == new.perfume_id)))
            .mappings()
            .one()
        )
        photos = (
            (
                await session.execute(
                    select(perfume_photos)
                    .where(perfume_photos.c.perfume_id == new.perfume_id)
                    .order_by(perfume_photos.c.position)
                )
            )
            .mappings()
            .all()
        )
        new_presentations = (
            (
                await session.execute(
                    select(presentations.c.ml).where(presentations.c.perfume_id == new.perfume_id)
                )
            )
            .scalars()
            .all()
        )
        updated = (
            (await session.execute(select(perfumes).where(perfumes.c.id == existing)))
            .mappings()
            .one()
        )
        updated_presentations = {
            row.ml: (row.id, row.price_cents)
            for row in (
                await session.execute(
                    select(
                        presentations.c.id, presentations.c.ml, presentations.c.price_cents
                    ).where(presentations.c.perfume_id == existing)
                )
            )
        }
    assert created["slug"] == slugify(f"{BRAND_NAME} Zz Imp New One {CONCENTRATION_ABBREVIATION}")
    assert (created["is_published"], created["gender"], created["description"]) == (
        True,
        "women",
        "Desde el Excel",
    )
    assert sorted(new_presentations) == [50, 100]
    assert [(p["position"], p["source_url"], p["card_key"], p["detail_key"]) for p in photos] == [
        (n, planned.source_url, planned.card_key, planned.detail_key)
        for n, planned in enumerate(new.photos, start=1)
    ]
    assert [p["id"] for p in photos] == [planned.photo_id for planned in new.photos]
    assert updated["is_published"] is True
    assert updated_presentations[100] == (existing_presentation, 199_900)  # same id, new price
    assert updated_presentations[30][1] == 90_000
    item = await _load_import(container, import_id)
    assert item.status is ImportStatus.APPLIED
    assert item.applied_by == ACTOR
    assert item.applied_at is not None
    assert item.plan is None


# --- repository: photos and source_url --------------------------------------------------------


def _perfume_for(world: World, name: str) -> Perfume:  # noqa: F811
    return Perfume.create(
        brand_id=world.brand,
        concentration_id=world.concentration,
        family_id=world.family,
        name=PerfumeName(name),
        slug=slugify(f"{name} {new_id()}"),
        gender=Gender.MEN,
        description=Description(""),
        notes=Notes((), (), ()),
        now=datetime.now(UTC),
    )


async def test_add_stores_the_photos_with_their_source_url_and_a_panel_photo_stays_null(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = _perfume_for(world, "Zz Imp Repo")
    now = datetime.now(UTC)
    for _position, url in enumerate(
        ("https://img.test/a.jpg?x=1&y=ñ", None, "https://img.test/c.jpg")
    ):
        photo_id = new_id()
        card, detail = photo_keys(perfume.id, photo_id)
        assert isinstance(
            perfume.add_photo(photo_id, card, detail, 1200, 900, created_at=now, source_url=url),
            Ok,
        )
    runner = SqlTransactionRunner(container.database)
    repository = SqlPerfumeRepository(container.database)
    loaded: list[Perfume] = []

    async def write() -> Ok[None]:
        assert isinstance(await repository.add(perfume), Ok)
        return Ok(None)

    async def read() -> Ok[None]:
        found = await repository.get_for_update(perfume.id)
        assert found is not None
        loaded.append(found)
        return Ok(None)

    await runner.run(write)
    await runner.run(read)

    assert [(p.id, p.position, p.source_url) for p in loaded[0].photos] == [
        (photo.id, photo.position, photo.source_url) for photo in perfume.photos
    ]
    assert [p.source_url for p in loaded[0].photos] == [
        "https://img.test/a.jpg?x=1&y=ñ",
        None,
        "https://img.test/c.jpg",
    ]


async def test_replace_photo_is_saved_in_the_same_position_and_the_old_row_is_gone(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume_id = await seed_perfume(container, world, "Zz Imp Repl")
    first = await seed_photo(container, perfume_id, 1, source_url="https://img.test/1.jpg")
    second = await seed_photo(container, perfume_id, 2, source_url="https://img.test/2.jpg")
    third = await seed_photo(container, perfume_id, 3, source_url="https://img.test/3.jpg")
    runner = SqlTransactionRunner(container.database)
    repository = SqlPerfumeRepository(container.database)
    replacement = new_id()
    card, detail = photo_keys(perfume_id, replacement)

    async def work() -> Ok[None]:
        perfume = await repository.get_for_update(perfume_id)
        assert perfume is not None
        assert isinstance(
            perfume.replace_photo(
                second,
                replacement,
                card,
                detail,
                800,
                1000,
                source_url="https://img.test/new.jpg",
                created_at=datetime.now(UTC),
            ),
            Ok,
        )
        assert isinstance(await repository.save(perfume), Ok)
        return Ok(None)

    await runner.run(work)

    async with container.database.reader() as session:
        rows = (
            (
                await session.execute(
                    select(perfume_photos)
                    .where(perfume_photos.c.perfume_id == perfume_id)
                    .order_by(perfume_photos.c.position)
                )
            )
            .mappings()
            .all()
        )
    assert [(r["position"], r["id"]) for r in rows] == [(1, first), (2, replacement), (3, third)]
    assert rows[1]["source_url"] == "https://img.test/new.jpg"
    assert (rows[1]["card_key"], rows[1]["detail_key"], rows[1]["width"]) == (card, detail, 800)
    assert rows[0]["source_url"] == "https://img.test/1.jpg"  # untouched photos keep theirs


# --- objects in RustFS ------------------------------------------------------------------------


async def _put(bucket: Bucket, perfume_id: UUID, *photo_ids: UUID) -> list[str]:
    keys: list[str] = []
    for photo_id in photo_ids:
        for key in photo_keys(perfume_id, photo_id):
            await bucket.storage.put(key, b"webp-bytes", "image/webp")
            keys.append(key)
    bucket.created_prefixes.append(f"perfumes/{perfume_id}/")
    return keys


async def test_replacing_a_photo_deletes_the_old_objects_and_keeps_the_staged_ones_live(
    container: Container,
    world: World,  # noqa: F811
    bucket: Bucket,
) -> None:
    perfume_id = await seed_perfume(container, world, "Zz Imp Obj", presentation_ml=(100,))
    presentation_id = await _presentation_id(container, perfume_id, 100)
    old_photo = await seed_photo(container, perfume_id, 1, source_url="https://img.test/old.jpg")
    old_keys = await _put(bucket, perfume_id, old_photo)
    replacement = _photo(perfume_id, "https://img.test/new.jpg", 1, replaces=old_photo)
    staged = [replacement.card_key, replacement.detail_key]
    for key in staged:
        await bucket.storage.put(key, b"staged", "image/webp")
    update = _planned(
        world,
        perfume_id,
        "Zz Imp Obj",
        is_new=False,
        first_row=2,
        presentations_=(_presentation(100, 250_000, presentation_id=presentation_id),),
        photos=(replacement,),
    )
    import_id = await _store_ready(container, update)
    assert all(bucket.public_get(key).status_code == 200 for key in old_keys + staged)

    result = await container.services.get(ConfirmCatalogImport).execute(
        import_id, ACTOR, may_record_stock=True
    )

    assert isinstance(result, Ok), result
    assert [bucket.public_get(key).status_code for key in old_keys] == [404, 404]
    assert [bucket.public_get(key).status_code for key in staged] == [200, 200]
    async with container.database.reader() as session:
        rows = (
            await session.execute(
                select(
                    perfume_photos.c.id, perfume_photos.c.position, perfume_photos.c.source_url
                ).where(perfume_photos.c.perfume_id == perfume_id)
            )
        ).all()
    assert [tuple(r) for r in rows] == [(replacement.photo_id, 1, "https://img.test/new.jpg")]


async def test_a_stale_confirm_deletes_no_object_neither_old_nor_staged(
    container: Container,
    world: World,  # noqa: F811
    bucket: Bucket,
) -> None:
    perfume_id = await seed_perfume(container, world, "Zz Imp Stale", presentation_ml=(100,))
    presentation_id = await _presentation_id(container, perfume_id, 100)
    old_photo = await seed_photo(container, perfume_id, 1)
    old_keys = await _put(bucket, perfume_id, old_photo)
    replacement = _photo(perfume_id, "https://img.test/new.jpg", 1, replaces=old_photo)
    staged = [replacement.card_key, replacement.detail_key]
    for key in staged:
        await bucket.storage.put(key, b"staged", "image/webp")
    update = _planned(
        world,
        perfume_id,
        "Zz Imp Stale",
        is_new=False,
        first_row=2,
        presentations_=(_presentation(100, 250_000, presentation_id=presentation_id),),
        photos=(replacement,),
    )
    import_id = await _store_ready(container, update)
    async with container.database.engine.begin() as connection:  # archived since the validation
        await connection.execute(
            text("UPDATE catalog.perfumes SET is_archived = true WHERE id = :id"),
            {"id": perfume_id},
        )

    result = await container.services.get(ConfirmCatalogImport).execute(
        import_id, ACTOR, may_record_stock=True
    )

    assert _issues(result) == [(2, "CATALOG_PERFUME_ARCHIVED")]
    assert [bucket.public_get(key).status_code for key in old_keys + staged] == [200] * 4


async def test_discard_deletes_the_staged_objects_and_marks_the_import_discarded(
    container: Container,
    world: World,  # noqa: F811
    bucket: Bucket,
) -> None:
    new = _new_perfume(world, "Zz Imp Disc", 2, photo_urls=("https://img.test/1.jpg",))
    staged = [new.photos[0].card_key, new.photos[0].detail_key]
    for key in staged:
        await bucket.storage.put(key, b"staged", "image/webp")
    bucket.created_prefixes.append(f"perfumes/{new.perfume_id}/")
    import_id = await _store_ready(container, new)
    assert [bucket.public_get(key).status_code for key in staged] == [200, 200]
    before = await _dump(container)

    result = await container.services.get(DiscardCatalogImport).execute(import_id)

    assert isinstance(result, Ok), result
    assert [bucket.public_get(key).status_code for key in staged] == [404, 404]
    item = await _load_import(container, import_id)
    assert (item.status, item.plan) == (ImportStatus.DISCARDED, None)
    assert item.discarded_at is not None
    after = await _dump(container)
    assert {t: rows for t, rows in after.items() if t != "catalog.imports"} == {
        t: rows for t, rows in before.items() if t != "catalog.imports"
    }
    again = await container.services.get(DiscardCatalogImport).execute(import_id)
    assert _code(again) == "CATALOG_IMPORT_NOT_READY"


# --- concurrency: one winner ------------------------------------------------------------------


async def test_concurrent_confirms_of_the_same_import_have_exactly_one_winner(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    new = _new_perfume(world, "Zz Imp Race", 2, photo_urls=("https://img.test/1.jpg",))
    import_id = await _store_ready(container, new)
    confirm = container.services.get(ConfirmCatalogImport)

    results = await asyncio.gather(
        *(confirm.execute(import_id, f"admin-{n}", may_record_stock=True) for n in range(4))
    )

    winners = [r for r in results if isinstance(r, Ok)]
    losers = [_code(r) for r in results if isinstance(r, Err)]
    assert len(winners) == 1
    assert losers == ["CATALOG_IMPORT_NOT_READY"] * 3  # never STALE: the lock serialized them
    async with container.database.reader() as session:
        count = await session.scalar(
            select(func.count()).select_from(perfumes).where(perfumes.c.id == new.perfume_id)
        )
    assert count == 1
    item = await _load_import(container, import_id)
    assert item.status is ImportStatus.APPLIED
    assert item.applied_by in {f"admin-{n}" for n in range(4)}


async def test_confirms_touching_the_same_perfumes_in_opposite_order_do_not_deadlock(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    """Repair round 1: the confirm locks perfumes in ascending id, whatever the file order.

    A gate makes the old (file order) behavior deadlock deterministically: after its first
    perfume lock each confirm waits, up to half a second, for the other one to hold its own
    first lock. With one global order the second confirm is blocked on the first lock and
    never arrives, so the gate times out and the first one goes on."""
    names = {
        await seed_perfume(container, world, "Zz Imp Lock A", presentation_ml=(100,)): (
            "Zz Imp Lock A"
        ),
        await seed_perfume(container, world, "Zz Imp Lock B", presentation_ml=(100,)): (
            "Zz Imp Lock B"
        ),
    }
    low, high = sorted(names)

    async def update(perfume_id: UUID, price: int, row: int) -> PlannedPerfume:
        presentation_id = await _presentation_id(container, perfume_id, 100)
        return _planned(
            world,
            perfume_id,
            names[perfume_id],
            is_new=False,
            first_row=row,
            presentations_=(_presentation(100, price, presentation_id=presentation_id, row=row),),
        )

    ascending = await _store_ready(
        container, await update(low, 111_100, 2), await update(high, 111_100, 3)
    )
    descending = await _store_ready(
        container, await update(high, 222_200, 2), await update(low, 222_200, 3)
    )
    arrived = 0
    both_locked = asyncio.Event()

    class Gated(SqlPerfumeRepository):
        def __init__(self, database: Database) -> None:
            super().__init__(database)
            self._first = True

        async def get_for_update(self, perfume_id: UUID) -> Perfume | None:
            nonlocal arrived
            found = await super().get_for_update(perfume_id)
            if self._first:
                self._first = False
                arrived += 1
                if arrived == 2:
                    both_locked.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(both_locked.wait(), timeout=0.5)
            return found

    def gated_confirm() -> ConfirmCatalogImport:
        return _confirm_with(
            container, SqlTransactionRunner(container.database), Gated(container.database)
        )

    results = await asyncio.gather(
        gated_confirm().execute(ascending, "admin-1", may_record_stock=True),
        gated_confirm().execute(descending, "admin-2", may_record_stock=True),
    )  # a deadlock would raise out of the gather

    assert any(isinstance(r, Ok) for r in results), results
    for result in results:
        assert isinstance(result, Ok) or _code(result) in {
            "CATALOG_IMPORT_STALE",
            "CATALOG_IMPORT_NOT_READY",
        }
    statuses = {
        (await _load_import(container, import_id)).status for import_id in (ascending, descending)
    }
    assert ImportStatus.APPLIED in statuses


async def test_confirms_creating_the_same_new_perfume_with_different_ids_do_not_deadlock(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    """Regression (finding catalog-import-confirm-same-new-perfume-deadlock): two imports
    create the same new perfume, each with its own pre-assigned id (one below and one above
    the existing perfume's id), and both update the same existing perfume. Ordered by id, one
    would insert the new perfume and then lock the existing one, while the other locks the
    existing one and then waits on the unique identity index. The gate makes that cycle
    deterministic; ordered by identity both take the same first step and simply queue."""
    existing = await seed_perfume(container, world, "Zz Imp Same Old", presentation_ml=(100,))
    presentation_id = await _presentation_id(container, existing, 100)

    def plan(new_perfume_id: UUID, price: int) -> tuple[PlannedPerfume, PlannedPerfume]:
        new = _planned(
            world,
            new_perfume_id,
            "Zz Imp Same New",
            is_new=True,
            first_row=2,
            presentations_=(_presentation(100, price, row=2),),
        )
        update = _planned(
            world,
            existing,
            "Zz Imp Same Old",
            is_new=False,
            first_row=3,
            presentations_=(_presentation(100, price, presentation_id=presentation_id, row=3),),
        )
        return new, update

    low_new = await _store_ready(container, *plan(UUID(int=1), 111_100))
    high_new = await _store_ready(container, *plan(UUID(int=2**128 - 1), 222_200))
    arrived = 0
    both_started = asyncio.Event()

    class Gated(SqlPerfumeRepository):
        def __init__(self, database: Database) -> None:
            super().__init__(database)
            self._first = True

        async def _after_first_step(self) -> None:
            nonlocal arrived
            if self._first:
                self._first = False
                arrived += 1
                if arrived == 2:
                    both_started.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(both_started.wait(), timeout=0.5)

        async def add(self, perfume: Perfume) -> Any:
            result = await super().add(perfume)
            await self._after_first_step()
            return result

        async def get_for_update(self, perfume_id: UUID) -> Perfume | None:
            found = await super().get_for_update(perfume_id)
            await self._after_first_step()
            return found

    def gated_confirm() -> ConfirmCatalogImport:
        return _confirm_with(
            container, SqlTransactionRunner(container.database), Gated(container.database)
        )

    results = await asyncio.gather(
        gated_confirm().execute(low_new, "admin-1", may_record_stock=True),
        gated_confirm().execute(high_new, "admin-2", may_record_stock=True),
    )  # a deadlock would raise out of the gather

    assert sum(isinstance(r, Ok) for r in results) == 1, results
    loser = next(r for r in results if not isinstance(r, Ok))
    assert _code(loser) == "CATALOG_IMPORT_STALE"


async def test_a_confirm_waits_for_the_expiry_that_holds_the_import_and_is_then_refused(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    new = _new_perfume(world, "Zz Imp Expire", 2)
    import_id = await _store_ready(container, new)
    runner = SqlTransactionRunner(container.database)
    repository = SqlCatalogImportRepository(container.database)
    holding, release = asyncio.Event(), asyncio.Event()

    async def expire_slowly() -> Ok[None]:
        locked = await repository.get_for_update(import_id)
        assert locked is not None
        holding.set()
        await release.wait()
        locked.expire()
        await repository.save(locked)
        return Ok(None)

    expiring: asyncio.Task[Result[None, Any]] = asyncio.create_task(runner.run(expire_slowly))
    await holding.wait()
    confirming: asyncio.Task[Result[None, DomainError]] = asyncio.create_task(
        container.services.get(ConfirmCatalogImport).execute(
            import_id, ACTOR, may_record_stock=True
        )
    )
    await asyncio.sleep(0.3)
    assert not confirming.done()  # blocked on the row lock
    release.set()
    await expiring
    result = await asyncio.wait_for(confirming, timeout=10)

    assert _code(result) == "CATALOG_IMPORT_NOT_READY"
    assert (await _load_import(container, import_id)).status is ImportStatus.EXPIRED
    async with container.database.reader() as session:
        count = await session.scalar(select(func.count()).select_from(perfumes))
    assert count == 0


async def test_the_expiry_job_skips_an_import_a_confirm_is_holding(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    new = _new_perfume(world, "Zz Imp Skip", 2)
    import_id = await _store_ready(container, new, expires_in=timedelta(minutes=10))
    runner = SqlTransactionRunner(container.database)
    repository = SqlCatalogImportRepository(container.database)
    holding, release = asyncio.Event(), asyncio.Event()

    async def confirm_slowly() -> Ok[None]:
        locked = await repository.get_for_update(import_id)
        assert locked is not None
        holding.set()
        await release.wait()
        locked.apply(datetime.now(UTC), ACTOR, {})
        await repository.save(locked)
        return Ok(None)

    confirming: asyncio.Task[Result[None, Any]] = asyncio.create_task(runner.run(confirm_slowly))
    await holding.wait()
    expire = ExpireCatalogImports(
        imports=repository,
        storage=InMemoryMediaStorage(),
        transactions=runner,
        clock=FixedClock(datetime.now(UTC) + timedelta(hours=3)),  # long past its expires_at
        stale_after=timedelta(minutes=30),
    )

    changed = await asyncio.wait_for(expire.run_once(), timeout=10)
    release.set()
    await confirming

    assert changed == 0  # SKIP LOCKED: the held import is not expired under the confirm's feet
    assert (await _load_import(container, import_id)).status is ImportStatus.APPLIED


# --- migration --------------------------------------------------------------------------------


@pytest.mark.skip(
    reason="NOT CONFIRMED: no automated downgrade/upgrade round trip of 0011 (it would rewrite "
    "the shared test schema mid-suite); the implementer ran it by hand and "
    "test_sql_catalog_imports.py asserts the upgraded columns"
)
def test_the_0011_migration_downgrades_and_upgrades_cleanly() -> None:
    raise AssertionError("not automated")
