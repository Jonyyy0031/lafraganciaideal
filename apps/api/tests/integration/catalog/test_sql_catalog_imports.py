"""Plan 007 against `fragancia_test`: the `catalog.imports` table through the SQL repository (JSON
round trip, claiming with SKIP LOCKED, the expiry selects), the read side, the SQL references
the validator uses (including `source_url`) and the migration's schema."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import inspect, select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.catalog_imports import StartCatalogImport
from fragancia_api.modules.catalog.domain import import_cells as cells
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportIssue,
    ImportPlan,
    ImportStatus,
    ImportSummary,
    PlannedPerfume,
    PlannedPhoto,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.errors import ImportFailed, ImportRequired
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import Gender, photo_keys
from fragancia_api.modules.catalog.infrastructure.sql_catalog_import_queries import (
    SqlCatalogImportQueries,
)
from fragancia_api.modules.catalog.infrastructure.sql_catalog_import_repository import (
    SqlCatalogImportRepository,
)
from fragancia_api.modules.catalog.infrastructure.sql_import_references import SqlImportReferences
from fragancia_api.modules.catalog.infrastructure.tables import imports
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Ok, new_id
from tests.integration.catalog.import_fixtures import (
    CONCENTRATION_ABBREVIATION,
    CONCENTRATION_NAME,
    FAMILY_NAME,
    OLD_FAMILY_NAME,
    World,
    seed_perfume,
    seed_photo,
    world,  # noqa: F401  (a fixture)
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
CANCUN = timedelta(hours=-5)
XLSX = b"PK\x03\x04" + bytes(range(256)) * 4  # arbitrary bytes, NUL and high bytes included


def _plan() -> ImportPlan:
    perfume_id, new_photo, old_photo = new_id(), new_id(), new_id()
    card, detail = photo_keys(perfume_id, new_photo)

    starts_at, ends_at = cells.sale_window(
        datetime(2026, 11, 1).date(), datetime(2026, 11, 30).date()
    )
    return ImportPlan(
        perfumes=(
            PlannedPerfume(
                perfume_id=perfume_id,
                is_new=False,
                brand_id=new_id(),
                concentration_id=new_id(),
                family_id=new_id(),
                name="Éros ñandú",
                gender=Gender.UNISEX,
                description="Fresco — “amaderado”",
                top_notes=("Bergamota", "Limón"),
                heart_notes=(),
                base_notes=("Ámbar",),
                publish=False,
                first_row=2,
                presentations=(
                    PlannedPresentation(
                        presentation_id=new_id(),
                        ml=100,
                        price_cents=250_000,
                        sale_price_cents=199_999,
                        sale_starts_at=starts_at,
                        sale_ends_at=ends_at,
                        availability="made_to_order",
                        lead_time_min_days=3,
                        lead_time_max_days=5,
                        row=2,
                    ),
                    PlannedPresentation(
                        None, 50, 150_000, None, None, None, "in_stock", None, None, 3
                    ),
                ),
                photos=(
                    PlannedPhoto(
                        2,
                        new_photo,
                        "https://img.test/a.jpg?x=1&y=ñ",
                        card,
                        detail,
                        1200,
                        900,
                        old_photo,
                    ),
                    PlannedPhoto(
                        3, new_id(), "https://img.test/b.jpg", "k1", "k2", 800, 1200, None
                    ),
                ),
            ),
            PlannedPerfume(
                perfume_id=new_id(),
                is_new=True,
                brand_id=new_id(),
                concentration_id=new_id(),
                family_id=new_id(),
                name="Nuevo",
                gender=Gender.WOMEN,
                description="",
                top_notes=(),
                heart_notes=(),
                base_notes=(),
                publish=None,
                first_row=4,
                presentations=(),
                photos=(),
            ),
        )
    )


def _pending(created_at: datetime, by: str = "admin-1") -> CatalogImport:
    return CatalogImport.create(XLSX, by, created_at)


def _validating(created_at: datetime, started_at: datetime) -> CatalogImport:
    item = _pending(created_at)
    item.start(started_at)
    return item


def _ready(created_at: datetime, expires_at: datetime) -> CatalogImport:
    item = _validating(created_at, created_at)
    item.finish_ready(
        expires_at - timedelta(hours=1),
        timedelta(hours=1),
        3,
        ImportSummary(1, 2, 3, 4, 5),
        _plan(),
    )
    return item


class Store:
    """The repository and a transaction runner over the real database."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.repository = SqlCatalogImportRepository(container.database)
        self.transactions = SqlTransactionRunner(container.database)
        self.queries = SqlCatalogImportQueries(container.database)

    async def add(self, *items: CatalogImport) -> None:
        async def work() -> Ok[None]:
            for item in items:
                await self.repository.add(item)
            return Ok(None)

        await self.transactions.run(work)

    async def load(self, import_id: UUID) -> CatalogImport:
        loaded: list[CatalogImport] = []

        async def work() -> Ok[None]:
            found = await self.repository.get_for_update(import_id)
            assert found is not None
            loaded.append(found)
            return Ok(None)

        await self.transactions.run(work)
        return loaded[0]

    async def save(self, item: CatalogImport) -> None:
        async def work() -> Ok[None]:
            await self.repository.save(item)
            return Ok(None)

        await self.transactions.run(work)


@pytest.fixture
async def store(container: Container, world: World) -> Store:  # noqa: F811
    return Store(container)


# --- schema (migration 0010) ------------------------------------------------------------------


async def test_the_migration_created_the_imports_table_index_and_source_url_column(
    container: Container,
) -> None:
    async with container.database.engine.connect() as connection:

        def reflect(sync_connection: object) -> tuple[dict[str, object], list[str], list[str]]:
            inspector: Any = inspect(sync_connection)
            columns = {c["name"]: c for c in inspector.get_columns("imports", schema="catalog")}
            indexes = [
                index["name"] or "" for index in inspector.get_indexes("imports", schema="catalog")
            ]
            photo_columns = [
                c["name"] for c in inspector.get_columns("perfume_photos", schema="catalog")
            ]
            return columns, indexes, photo_columns

        columns, indexes, photo_columns = await connection.run_sync(reflect)

    assert set(columns) == {
        "id",
        "status",
        "file",
        "created_by",
        "created_at",
        "started_at",
        "finished_at",
        "expires_at",
        "row_count",
        "error_count",
        "summary",
        "errors",
        "plan",
        "applied_at",
        "applied_by",
        "discarded_at",
    }
    assert "ix_catalog_imports_status_created_at" in indexes
    assert "source_url" in photo_columns


# --- repository: round trip -------------------------------------------------------------------


async def test_a_pending_import_round_trips_with_its_binary_file(store: Store) -> None:
    item = _pending(T0, by="admin-9")
    await store.add(item)

    loaded = await store.load(item.id)

    assert loaded.status is ImportStatus.PENDING
    assert loaded.file == XLSX
    assert (loaded.created_by, loaded.created_at) == ("admin-9", T0)
    assert (loaded.started_at, loaded.finished_at, loaded.expires_at) == (None, None, None)
    assert (loaded.row_count, loaded.error_count, loaded.summary, loaded.plan) == (
        None,
        None,
        None,
        None,
    )
    assert loaded.errors == ()


async def test_a_five_megabyte_file_is_stored_and_read_back_intact(store: Store) -> None:
    big = b"PK\x03\x04" + bytes(range(256)) * (5_242_880 // 256 - 1)
    item = CatalogImport.create(big, "admin-1", T0)
    await store.add(item)

    loaded = await store.load(item.id)

    assert loaded.file == big
    assert len(loaded.file) == len(big)


async def test_a_ready_import_round_trips_its_summary_and_the_whole_typed_plan(
    store: Store,
) -> None:
    item = _ready(T0, T0 + timedelta(hours=1))
    await store.add(item)

    loaded = await store.load(item.id)

    assert loaded.status is ImportStatus.READY
    assert loaded.summary == ImportSummary(1, 2, 3, 4, 5)
    assert loaded.plan == item.plan  # frozen dataclasses: UUIDs, enums, tuples, datetimes
    assert loaded.plan is not None
    sale = loaded.plan.perfumes[0].presentations[0]
    assert sale.sale_starts_at is not None
    assert sale.sale_ends_at is not None
    assert sale.sale_starts_at.isoformat() == "2026-11-01T00:00:00-05:00"
    assert sale.sale_ends_at.isoformat() == "2026-12-01T00:00:00-05:00"
    assert sale.sale_starts_at.utcoffset() == CANCUN
    assert loaded.plan.perfumes[0].photos[0].replaces_photo_id is not None
    assert loaded.plan.perfumes[0].gender is Gender.UNISEX
    assert item.plan is not None
    assert loaded.plan.staged_keys() == item.plan.staged_keys()
    assert (loaded.row_count, loaded.error_count, loaded.errors) == (3, 0, ())
    assert loaded.file is None
    assert loaded.expires_at == item.expires_at
    assert loaded.finished_at == item.finished_at


async def test_the_plan_is_stored_as_plain_json_with_string_ids_and_iso_dates(
    store: Store, container: Container
) -> None:
    item = _ready(T0, T0 + timedelta(hours=1))
    await store.add(item)

    async with container.database.reader() as session:
        plan = (
            await session.execute(select(imports.c.plan).where(imports.c.id == item.id))
        ).scalar_one()

    perfume = plan["perfumes"][0]
    assert perfume["perfume_id"] == str(item.plan.perfumes[0].perfume_id)  # type: ignore[union-attr]
    assert perfume["gender"] == "unisex"
    assert perfume["presentations"][0]["sale_starts_at"] == "2026-11-01T00:00:00-05:00"
    assert perfume["presentations"][1]["presentation_id"] is None
    assert perfume["top_notes"] == ["Bergamota", "Limón"]


async def test_issues_round_trip_with_null_row_and_column_and_unicode_messages(
    store: Store,
) -> None:
    item = _validating(T0, T0)
    issues = [
        ImportIssue.of(ImportRequired(), row=2, column="Concentración"),
        ImportIssue(None, None, "CATALOG_IMPORT_VALUE_INVALID", "Ñandú — “raro”"),
    ]
    item.finish_invalid(T0, 5, issues)
    await store.add(item)

    loaded = await store.load(item.id)

    assert loaded.status is ImportStatus.INVALID
    assert loaded.errors == tuple(issues)
    assert (loaded.error_count, loaded.row_count) == (2, 5)
    assert loaded.plan is None


async def test_a_failed_import_round_trips_its_single_issue(store: Store) -> None:
    item = _pending(T0)
    item.fail(T0, ImportIssue.of(ImportFailed(), row=None, column=None))
    await store.add(item)

    loaded = await store.load(item.id)

    assert loaded.status is ImportStatus.FAILED
    assert [(e.row, e.column, e.code) for e in loaded.errors] == [
        (None, None, "CATALOG_IMPORT_FAILED")
    ]
    assert loaded.file is None


async def test_a_thousand_issues_are_kept_and_the_full_count_stays(store: Store) -> None:
    item = _validating(T0, T0)
    item.finish_invalid(
        T0, 2000, [ImportIssue(n, "Marca", "CATALOG_IMPORT_REQUIRED", "x") for n in range(2, 1503)]
    )
    await store.add(item)

    loaded = await store.load(item.id)

    assert len(loaded.errors) == 1000
    assert loaded.error_count == 1501


async def test_saving_updates_the_state_but_never_the_creator_or_the_creation_time(
    store: Store,
) -> None:
    item = _pending(T0, by="admin-1")
    await store.add(item)
    loaded = await store.load(item.id)
    loaded.start(T0 + timedelta(minutes=1))
    loaded.created_by = "someone-else"
    loaded.created_at = T0 + timedelta(days=9)

    await store.save(loaded)

    again = await store.load(item.id)
    assert again.status is ImportStatus.VALIDATING
    assert again.started_at == T0 + timedelta(minutes=1)
    assert (again.created_by, again.created_at) == ("admin-1", T0)


async def test_the_start_command_stores_a_pending_import_through_sql(store: Store) -> None:
    command = StartCatalogImport(
        imports=store.repository, transactions=store.transactions, clock=FixedClock(T0)
    )

    result = await command.execute(XLSX, "admin-3")

    assert isinstance(result, Ok)
    loaded = await store.load(result.value)
    assert (loaded.status, loaded.file, loaded.created_by, loaded.created_at) == (
        ImportStatus.PENDING,
        XLSX,
        "admin-3",
        T0,
    )


async def test_getting_an_unknown_import_for_update_is_none(store: Store) -> None:
    async def work() -> Ok[None]:
        assert await store.repository.get_for_update(UUID(int=404)) is None
        return Ok(None)

    await store.transactions.run(work)


# --- repository: claiming ---------------------------------------------------------------------


async def _claim(store: Store, hold: asyncio.Event | None = None) -> UUID | None:
    claimed: list[UUID | None] = []

    async def work() -> Ok[None]:
        item = await store.repository.claim_next_pending()
        claimed.append(item.id if item is not None else None)
        if hold is not None:
            await hold.wait()  # keep the row lock until told to commit
        return Ok(None)

    await store.transactions.run(work)
    return claimed[0]


async def test_claiming_takes_the_oldest_pending_import_by_creation_time(store: Store) -> None:
    newest = _pending(T0 + timedelta(minutes=2))
    oldest = _pending(T0)
    middle = _pending(T0 + timedelta(minutes=1))
    await store.add(newest, oldest, middle)

    assert await _claim(store) == oldest.id


async def test_claiming_ignores_imports_that_are_not_pending(store: Store) -> None:
    only_pending = _pending(T0 + timedelta(hours=1))
    await store.add(
        _validating(T0, T0),
        _ready(T0 + timedelta(minutes=1), T0 + timedelta(hours=2)),
        only_pending,
    )

    assert await _claim(store) == only_pending.id


async def test_claiming_with_nothing_pending_is_none(store: Store) -> None:
    await store.add(_validating(T0, T0))

    assert await _claim(store) is None


async def test_two_concurrent_claims_get_different_imports_and_a_third_gets_none(
    store: Store,
) -> None:
    first, second = _pending(T0), _pending(T0 + timedelta(minutes=1))
    await store.add(first, second)
    release = asyncio.Event()

    holder_1 = asyncio.create_task(_claim(store, release))
    await asyncio.sleep(0.3)  # the first claim now holds the lock on the oldest row
    holder_2 = asyncio.create_task(_claim(store, release))
    await asyncio.sleep(0.3)  # the second one skipped the locked row and holds the next one
    third = await asyncio.wait_for(_claim(store, None), timeout=5)  # must not block either
    release.set()
    held, other = await holder_1, await holder_2

    assert held == first.id
    assert other == second.id  # SKIP LOCKED: did not wait for the locked row
    assert third is None  # both rows are locked: nothing to take


async def test_a_claimed_row_stays_pending_until_the_claimer_saves_it(store: Store) -> None:
    item = _pending(T0)
    await store.add(item)

    claimed = await _claim(store)
    again = await _claim(store)

    assert claimed == again == item.id  # nobody saved `validating`, so it is still pending


# --- repository: the expiry selects -----------------------------------------------------------


async def _listed(store: Store, select_name: str, moment: datetime) -> list[UUID]:
    ids: list[UUID] = []

    async def work() -> Ok[None]:
        method = getattr(store.repository, select_name)
        ids.extend(item.id for item in await method(moment))
        return Ok(None)

    await store.transactions.run(work)
    return ids


async def test_stale_validating_returns_only_validating_imports_started_before_the_limit(
    store: Store,
) -> None:
    stale = _validating(T0, T0 - timedelta(minutes=31))
    boundary = _validating(T0, T0 - timedelta(minutes=30))
    fresh = _validating(T0, T0 - timedelta(minutes=29))
    await store.add(stale, boundary, fresh, _pending(T0), _ready(T0, T0 + timedelta(hours=1)))

    found = await _listed(store, "list_stale_validating", T0 - timedelta(minutes=30))

    assert found == [stale.id]  # strictly before the limit


async def test_expired_ready_returns_only_ready_imports_expiring_at_or_before_now(
    store: Store,
) -> None:
    past = _ready(T0 - timedelta(hours=3), T0 - timedelta(minutes=1))
    exact = _ready(T0 - timedelta(hours=2), T0)
    future = _ready(T0 - timedelta(hours=1), T0 + timedelta(minutes=1))
    await store.add(past, exact, future, _validating(T0, T0), _pending(T0))

    found = await _listed(store, "list_expired_ready", T0)

    assert found == [past.id, exact.id]  # oldest first


async def test_the_expiry_selects_skip_rows_another_transaction_has_locked(store: Store) -> None:
    locked, free = _ready(T0 - timedelta(hours=3), T0), _ready(T0 - timedelta(hours=2), T0)
    await store.add(locked, free)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async def work() -> Ok[None]:
            await store.repository.get_for_update(locked.id)
            holding.set()
            await release.wait()
            return Ok(None)

        await store.transactions.run(work)

    task = asyncio.create_task(hold())
    await holding.wait()
    found = await asyncio.wait_for(_listed(store, "list_expired_ready", T0), timeout=5)
    release.set()
    await task

    assert found == [free.id]


# --- read side --------------------------------------------------------------------------------


async def test_the_query_maps_a_ready_import_to_the_response(store: Store) -> None:
    item = _ready(T0, T0 + timedelta(hours=1))
    await store.add(item)

    response = await store.queries.get(item.id)

    assert response is not None
    assert (response.id, response.status) == (item.id, "ready")
    assert response.created_at == T0
    assert response.expires_at == T0 + timedelta(hours=1)
    assert response.row_count == 3
    assert response.summary is not None
    assert response.summary.model_dump() == {
        "perfumes_new": 1,
        "perfumes_updated": 2,
        "presentations_new": 3,
        "presentations_updated": 4,
        "photos_new": 5,
    }
    assert (response.error_count, response.errors) == (0, [])


async def test_the_query_returns_the_capped_issues_and_the_full_error_count(store: Store) -> None:
    item = _validating(T0, T0)
    item.finish_invalid(
        T0, 2000, [ImportIssue(n, "Marca", "CATALOG_IMPORT_REQUIRED", "m") for n in range(2, 1203)]
    )
    await store.add(item)

    response = await store.queries.get(item.id)

    assert response is not None
    assert (response.status, response.error_count, len(response.errors)) == ("invalid", 1201, 1000)
    assert (response.errors[0].row, response.errors[0].column) == (2, "Marca")


async def test_the_query_of_a_pending_import_has_a_zero_error_count_and_no_report(
    store: Store,
) -> None:
    item = _pending(T0)
    await store.add(item)

    response = await store.queries.get(item.id)

    assert response is not None
    assert (response.status, response.error_count, response.errors, response.summary) == (
        "pending",
        0,
        [],
        None,
    )
    assert (response.finished_at, response.expires_at, response.row_count) == (None, None, None)


async def test_the_query_of_an_unknown_import_is_none(store: Store) -> None:
    assert await store.queries.get(UUID(int=404)) is None


# --- SQL references (what the validator reads) ------------------------------------------------


async def test_the_references_list_brands_families_and_concentrations_with_their_slugs(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    references = SqlImportReferences(container.database)

    brands = {item.id: item for item in await references.brands()}
    families = {item.id: item for item in await references.families()}
    concentrations = {item.id: item for item in await references.concentrations()}

    assert (brands[world.brand].slug, brands[world.brand].is_active) == ("zz-imp-brand", True)
    assert brands[world.brand].alt_slug is None
    assert (families[world.family].slug, families[world.family].name) == (
        slugify(FAMILY_NAME),
        FAMILY_NAME,
    )
    assert families[world.old_family].is_active is False
    assert families[world.old_family].name == OLD_FAMILY_NAME
    concentration = concentrations[world.concentration]
    assert concentration.name == CONCENTRATION_NAME
    assert concentration.slug == slugify(CONCENTRATION_NAME)
    assert concentration.alt_slug == slugify(CONCENTRATION_ABBREVIATION)  # the abbreviation slug
    assert concentration.is_active is True


async def test_no_identities_means_no_lookup(container: Container) -> None:
    assert await SqlImportReferences(container.database).existing_perfumes([]) == {}


async def test_existing_perfumes_returns_the_stored_ones_with_presentations_and_photos(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    references = SqlImportReferences(container.database)
    eros = await seed_perfume(
        container,
        world,
        "Eros",
        presentation_ml=(100, 50),
        archived_ml=(30,),
        family=world.other_family,
    )
    first = await seed_photo(container, eros, 1, source_url="https://img.test/one.jpg")
    second = await seed_photo(container, eros, 2)  # uploaded from the panel: no source URL
    old = await seed_perfume(container, world, "Old One", archived=True)
    await seed_perfume(container, world, "Not Asked")
    wanted = [
        (world.brand, "eros", world.concentration),
        (world.brand, "old-one", world.concentration),
        (world.brand, "never-stored", world.concentration),
        (new_id(), "eros", world.concentration),  # same name, another brand
        (world.brand, "eros", new_id()),  # same name, another concentration
    ]

    found = await references.existing_perfumes(wanted)

    assert set(found) == {wanted[0], wanted[1]}
    stored = found[wanted[0]]
    assert stored.id == eros
    assert stored.is_archived is False
    assert stored.family_id == world.other_family
    assert sorted(stored.presentations) == [30, 50, 100]
    assert [stored.presentations[ml][1] for ml in (30, 50, 100)] == [False, True, True]
    assert stored.photos == {1: (first, "https://img.test/one.jpg"), 2: (second, None)}
    archived = found[wanted[1]]
    assert (archived.id, archived.is_archived) == (old, True)
    assert (archived.presentations, archived.photos) == ({}, {})


async def test_a_perfume_without_presentations_or_photos_has_empty_maps(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    await seed_perfume(container, world, "Bare")
    key = (world.brand, "bare", world.concentration)

    found = await SqlImportReferences(container.database).existing_perfumes([key])

    assert (found[key].presentations, found[key].photos) == ({}, {})


async def test_the_source_url_column_holds_two_thousand_characters_and_defaults_to_null(
    container: Container,
    world: World,  # noqa: F811
) -> None:
    perfume = await seed_perfume(container, world, "Long Url")
    url = "https://img.test/" + "a" * (2000 - len("https://img.test/"))
    long_photo = await seed_photo(container, perfume, 1, source_url=url)
    plain_photo = await seed_photo(container, perfume, 2)
    key = (world.brand, "long-url", world.concentration)

    found = (await SqlImportReferences(container.database).existing_perfumes([key]))[key]

    assert found.photos[1] == (long_photo, url)
    assert found.photos[2] == (plain_photo, None)
    assert len(url) == 2000


async def test_the_imports_table_is_empty_between_tests(store: Store, container: Container) -> None:
    async with container.database.reader() as session:
        count = (await session.execute(text("SELECT count(*) FROM catalog.imports"))).scalar_one()

    assert count == 0
