"""Plan 008, application layer: `ConfirmCatalogImport` and `DiscardCatalogImport` with in-memory
adapters.

`InMemoryTransactionRunner` never rolls back, so the "nothing saved on stale" tests run under
`_RollbackRunner`: it snapshots every store before the work and restores them when the work
returns an `Err` or raises, like `SqlTransactionRunner`. It also records what was written at
the moment of the rollback, which proves a stale confirm really wrote the valid perfumes
before it was undone (so the restore is what the assertions are checking).
"""

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog.testing

from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ConfirmCatalogImport,
    DiscardCatalogImport,
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
from fragancia_api.modules.catalog.domain.perfume import Gender, Perfume, photo_keys
from fragancia_api.modules.catalog.infrastructure.in_memory_imports import InMemoryCatalogImports
from fragancia_api.modules.catalog.infrastructure.in_memory_media import InMemoryMediaStorage
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, Result
from tests.unit.catalog.perfume_support import (
    World,
    add_presentation,
    error_code,
    error_details,
    make_brand,
    make_concentration,
    make_family,
    make_perfume,
    unwrap_ok,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=60)
ACTOR = "admin-9"
URL_A = "https://img.test/a.jpg"
URL_B = "https://img.test/b.jpg"
URL_C = "https://img.test/c.jpg"
URL_D = "https://img.test/d.jpg"


class _RollbackRunner(InMemoryTransactionRunner):
    """Restores the given stores when the work fails (an `Err` or an exception), and records
    the state it found right before undoing it, plus when the transaction ended."""

    def __init__(self, storage: InMemoryMediaStorage, **stores: dict[Any, Any]) -> None:
        self._storage = storage
        self._stores = stores
        self.rolled_back = False
        self.committed = False
        self.before_rollback: dict[str, dict[Any, Any]] = {}
        self.deleted_at_end: list[str] | None = None

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        snapshot = {name: copy.deepcopy(store) for name, store in self._stores.items()}
        try:
            result = await work()
        except BaseException:
            self._undo(snapshot)
            raise
        self.deleted_at_end = list(self._storage.deleted)
        if isinstance(result, Err):
            self._undo(snapshot)
        else:
            self.committed = True
        return result

    def _undo(self, snapshot: dict[str, dict[Any, Any]]) -> None:
        self.rolled_back = True
        self.before_rollback = {n: copy.deepcopy(s) for n, s in self._stores.items()}
        for name, store in self._stores.items():
            store.clear()
            store.update(snapshot[name])


@dataclass
class Rig:
    world: World
    imports: InMemoryCatalogImports
    storage: InMemoryMediaStorage
    transactions: _RollbackRunner
    clock: FixedClock
    confirm: ConfirmCatalogImport
    discard: DiscardCatalogImport


def _rig(*, fail_deletes: bool = False, world: World | None = None) -> Rig:
    world = world or World()
    imports = InMemoryCatalogImports()
    storage = InMemoryMediaStorage(fail_deletes=fail_deletes)
    transactions = _RollbackRunner(
        storage,
        perfumes=world.perfumes.by_id,
        slug_history=world.perfumes.slug_history,
        imports=imports.by_id,
    )
    clock = FixedClock(NOW)
    confirm = ConfirmCatalogImport(
        imports=imports,
        perfumes=world.perfumes,
        brands=world.brands,
        families=world.families,
        concentrations=world.concentrations,
        storage=storage,
        transactions=transactions,
        clock=clock,
    )
    discard = DiscardCatalogImport(
        imports=imports, storage=storage, transactions=transactions, clock=clock
    )
    return Rig(world, imports, storage, transactions, clock, confirm, discard)


# --- plan builders ----------------------------------------------------------------------------


def _presentation(
    ml: int = 100, price: int = 250_000, *, presentation_id: UUID | None = None, row: int = 2
) -> PlannedPresentation:
    return PlannedPresentation(
        presentation_id, ml, price, None, None, None, "in_stock", None, None, row
    )


def _photo(
    perfume_id: UUID, url: str, position: int = 1, *, replaces: UUID | None = None
) -> PlannedPhoto:
    photo_id = UUID(int=perfume_id.int * 100 + position + (50 if replaces else 0))
    card, detail = photo_keys(perfume_id, photo_id)
    return PlannedPhoto(position, photo_id, url, card, detail, 1200, 900, replaces)


def _planned(
    rig: Rig,
    *,
    perfume_id: UUID,
    is_new: bool,
    name: str = "Eros",
    first_row: int = 2,
    presentations: tuple[PlannedPresentation, ...] = (),
    photos: tuple[PlannedPhoto, ...] = (),
    publish: bool | None = None,
    **overrides: Any,
) -> PlannedPerfume:
    values: dict[str, Any] = {
        "perfume_id": perfume_id,
        "is_new": is_new,
        "brand_id": rig.world.brand.id,
        "concentration_id": rig.world.concentration.id,
        "family_id": rig.world.family.id,
        "name": name,
        "gender": Gender.MEN,
        "description": "Fresco",
        "top_notes": ("Menta",),
        "heart_notes": (),
        "base_notes": (),
        "publish": publish,
        "first_row": first_row,
        "presentations": presentations,
        "photos": photos,
    }
    values.update(overrides)
    return PlannedPerfume(**values)


def _new(rig: Rig, index: int, name: str, **kwargs: Any) -> PlannedPerfume:
    perfume_id = UUID(int=index)
    kwargs.setdefault("presentations", (_presentation(row=kwargs.get("first_row", 2)),))
    kwargs.setdefault("photos", (_photo(perfume_id, URL_A),))
    return _planned(rig, perfume_id=perfume_id, is_new=True, name=name, **kwargs)


def _stored(rig: Rig, name: str = "Luna", *, ml: int = 100) -> tuple[Perfume, UUID]:
    perfume = make_perfume(rig.world.brand, rig.world.family, rig.world.concentration, name=name)
    presentation_id = add_presentation(perfume, ml=ml)
    rig.world.seed(perfume)
    return perfume, presentation_id


def _existing(
    rig: Rig, perfume: Perfume, presentation_id: UUID | None, **kwargs: Any
) -> PlannedPerfume:
    kwargs.setdefault(
        "presentations", (_presentation(ml=100, price=199_900, presentation_id=presentation_id),)
    )
    return _planned(rig, perfume_id=perfume.id, is_new=False, name=perfume.name.value, **kwargs)


def _ready(plan: ImportPlan, *, expires_at: datetime = NOW + TTL) -> CatalogImport:
    item = CatalogImport.create(b"PK\x03\x04data", "admin-1", expires_at - TTL - timedelta(hours=1))
    item.start(expires_at - TTL - timedelta(minutes=30))
    item.finish_ready(expires_at - TTL, TTL, 3, ImportSummary(1, 0, 1, 0, 1), plan)
    return item


async def _store(rig: Rig, *planned: PlannedPerfume, expires_at: datetime = NOW + TTL) -> UUID:
    item = _ready(ImportPlan(planned), expires_at=expires_at)
    await rig.imports.add(item)
    return item.id


def _issues(result: Result[Any, Any]) -> list[tuple[int | None, str]]:
    details = error_details(result)
    assert isinstance(details, dict)
    return [(issue["row"], issue["code"]) for issue in details["issues"]]


def _plain(value: Any) -> Any:
    """Aggregates define no `__eq__`: compare their state, recursively, as plain data."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {name: _plain(item) for name, item in vars(value).items()}
    return value


def _snapshot(rig: Rig) -> Any:
    return _plain(
        {
            "perfumes": rig.world.perfumes.by_id,
            "slug_history": rig.world.perfumes.slug_history,
            "imports": rig.imports.by_id,
        }
    )


# --- confirm: the happy path ------------------------------------------------------------------


async def test_confirming_a_valid_plan_creates_new_perfumes_with_the_planned_ids_and_photos() -> (
    None
):
    rig = _rig()
    planned = _new(
        rig,
        7,
        "Eros",
        presentations=(_presentation(100, 250_000), _presentation(50, 150_000, row=3)),
        photos=(_photo(UUID(int=7), URL_A, 1), _photo(UUID(int=7), URL_B, 2)),
    )
    import_id = await _store(rig, planned)

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    created = rig.world.perfumes.by_id[UUID(int=7)]
    assert created.slug == "versace-eros-edt"
    assert (created.brand_id, created.family_id, created.concentration_id) == (
        rig.world.brand.id,
        rig.world.family.id,
        rig.world.concentration.id,
    )
    assert created.is_published is False
    assert sorted(p.ml.value for p in created.presentations) == [50, 100]
    assert [(p.position, p.source_url, p.card_key, p.detail_key) for p in created.photos] == [
        (1, URL_A, planned.photos[0].card_key, planned.photos[0].detail_key),
        (2, URL_B, planned.photos[1].card_key, planned.photos[1].detail_key),
    ]
    assert [p.id for p in created.photos] == [p.photo_id for p in planned.photos]


async def test_confirming_marks_the_import_applied_with_the_actor_and_the_clock() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"))

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    item = rig.imports.by_id[import_id]
    assert item.status is ImportStatus.APPLIED
    assert (item.applied_at, item.applied_by, item.plan) == (NOW, ACTOR, None)
    assert rig.transactions.committed is True


async def test_confirming_updates_an_existing_perfume_and_keeps_its_presentation_id() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    planned = _existing(
        rig, stored, presentation_id, description="Nuevo texto", gender=Gender.WOMEN
    )
    import_id = await _store(rig, planned)

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    updated = rig.world.perfumes.by_id[stored.id]
    assert (updated.description.value, updated.gender) == ("Nuevo texto", Gender.WOMEN)
    [presentation] = updated.presentations
    assert presentation.id == presentation_id
    assert presentation.price.amount.cents == 199_900


async def test_confirming_adds_a_new_presentation_to_an_existing_perfume() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    planned = _existing(
        rig,
        stored,
        presentation_id,
        presentations=(
            _presentation(100, 250_000, presentation_id=presentation_id),
            _presentation(50, 150_000, row=3),
        ),
    )
    import_id = await _store(rig, planned)

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    updated = rig.world.perfumes.by_id[stored.id]
    assert sorted(p.ml.value for p in updated.presentations) == [50, 100]
    assert presentation_id in {p.id for p in updated.presentations}


async def test_the_publish_column_publishes_hides_or_leaves_the_flag() -> None:
    rig = _rig()
    published, published_presentation = _stored(rig, "Uno")
    hidden, hidden_presentation = _stored(rig, "Dos")
    untouched, untouched_presentation = _stored(rig, "Tres")
    hidden.is_published = True
    rig.world.seed(hidden)
    untouched.is_published = True
    rig.world.seed(untouched)
    import_id = await _store(
        rig,
        _existing(rig, published, published_presentation, publish=True, first_row=2),
        _existing(rig, hidden, hidden_presentation, publish=False, first_row=3),
        _existing(rig, untouched, untouched_presentation, publish=None, first_row=4),
        _new(rig, 7, "Nuevo", publish=True, first_row=5),
        _new(rig, 8, "Oculto", publish=None, first_row=6),
    )

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    by_id = rig.world.perfumes.by_id
    assert by_id[published.id].is_published is True
    assert by_id[published.id].first_published_at == NOW
    assert by_id[hidden.id].is_published is False
    assert by_id[untouched.id].is_published is True
    assert by_id[UUID(int=7)].is_published is True
    assert by_id[UUID(int=8)].is_published is False


async def test_a_replaced_photo_keeps_its_position_and_its_objects_go_after_the_commit() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    first, second, third = (UUID(int=900 + n) for n in range(3))
    for _number, photo_id in enumerate((first, second, third), start=1):
        card, detail = photo_keys(stored.id, photo_id)
        unwrap_ok(
            stored.add_photo(photo_id, card, detail, 1200, 900, created_at=NOW, source_url=URL_A)
        )
        rig.storage.objects[card] = b"c"
        rig.storage.objects[detail] = b"d"
    rig.world.seed(stored)
    old_keys = list(photo_keys(stored.id, second))
    planned = _existing(
        rig, stored, presentation_id, photos=(_photo(stored.id, URL_D, 2, replaces=second),)
    )
    import_id = await _store(rig, planned)

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    photos = rig.world.perfumes.by_id[stored.id].photos
    assert [p.position for p in photos] == [1, 2, 3]
    assert [p.id for p in photos] == [first, planned.photos[0].photo_id, third]
    assert photos[1].source_url == URL_D
    assert rig.storage.deleted == old_keys
    assert rig.transactions.deleted_at_end == []  # nothing deleted inside the transaction
    assert planned.photos[0].card_key not in rig.storage.deleted


async def test_a_confirm_without_replaced_photos_deletes_no_objects() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"))

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    assert rig.storage.deleted == []


async def test_a_failure_deleting_replaced_objects_does_not_undo_the_confirm() -> None:
    rig = _rig(fail_deletes=True)
    stored, presentation_id = _stored(rig)
    photo_id = UUID(int=901)
    card, detail = photo_keys(stored.id, photo_id)
    unwrap_ok(stored.add_photo(photo_id, card, detail, 1200, 900, created_at=NOW))
    rig.world.seed(stored)
    planned = _existing(
        rig, stored, presentation_id, photos=(_photo(stored.id, URL_D, 1, replaces=photo_id),)
    )
    import_id = await _store(rig, planned)

    with structlog.testing.capture_logs() as logs:
        unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    assert rig.imports.by_id[import_id].status is ImportStatus.APPLIED
    assert [log["event"] for log in logs] == ["catalog_import_objects_not_deleted"]


async def test_an_unchanged_archived_family_brand_and_concentration_do_not_block_an_update() -> (
    None
):
    rig = _rig()
    stored, presentation_id = _stored(rig)
    rig.world.family.is_active = False
    rig.world.brand.is_active = False
    rig.world.concentration.is_active = False
    import_id = await _store(rig, _existing(rig, stored, presentation_id))

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    assert rig.imports.by_id[import_id].status is ImportStatus.APPLIED


async def test_confirming_twice_applies_once_and_the_second_is_not_ready() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"))
    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))
    after_first = _snapshot(rig)

    second = await rig.confirm.execute(import_id, ACTOR)

    assert error_code(second) == "CATALOG_IMPORT_NOT_READY"
    assert _snapshot(rig) == after_first


# --- confirm: not found / not ready -----------------------------------------------------------


async def test_confirming_an_unknown_import_is_not_found() -> None:
    rig = _rig()

    assert error_code(await rig.confirm.execute(UUID(int=404), ACTOR)) == "CATALOG_IMPORT_NOT_FOUND"


def _with_status(item: CatalogImport, status: ImportStatus) -> CatalogImport:
    item.status = status
    return item


@pytest.mark.parametrize(
    "status",
    [
        ImportStatus.PENDING,
        ImportStatus.VALIDATING,
        ImportStatus.INVALID,
        ImportStatus.FAILED,
        ImportStatus.EXPIRED,
        ImportStatus.APPLIED,
        ImportStatus.DISCARDED,
    ],
)
async def test_confirming_an_import_that_is_not_ready_is_refused_and_writes_nothing(
    status: ImportStatus,
) -> None:
    rig = _rig()
    item = _with_status(_ready(ImportPlan((_new(rig, 7, "Eros"),))), status)
    await rig.imports.add(item)
    before = _snapshot(rig)

    result = await rig.confirm.execute(item.id, ACTOR)

    assert error_code(result) == "CATALOG_IMPORT_NOT_READY"
    assert _snapshot(rig) == before
    assert rig.storage.deleted == []


async def test_a_ready_import_past_expires_at_is_not_ready_even_if_the_cron_did_not_run() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"), expires_at=NOW - timedelta(seconds=1))
    before = _snapshot(rig)

    result = await rig.confirm.execute(import_id, ACTOR)

    assert error_code(result) == "CATALOG_IMPORT_NOT_READY"
    assert _snapshot(rig) == before


async def test_a_ready_import_at_expires_at_exactly_is_not_ready() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"), expires_at=NOW)

    assert error_code(await rig.confirm.execute(import_id, ACTOR)) == "CATALOG_IMPORT_NOT_READY"


async def test_a_ready_import_one_second_before_expires_at_is_confirmed() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"), expires_at=NOW + timedelta(seconds=1))

    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))


# --- confirm: every stale cause ---------------------------------------------------------------


async def _stale_issue(rig: Rig, planned: PlannedPerfume) -> tuple[int | None, str]:
    import_id = await _store(rig, planned)
    result = await rig.confirm.execute(import_id, ACTOR)
    assert error_code(result) == "CATALOG_IMPORT_STALE"
    [issue] = _issues(result)
    return issue


async def test_stale_an_existing_perfume_archived_since_the_validation() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    stored.archive()
    rig.world.seed(stored)

    issue = await _stale_issue(rig, _existing(rig, stored, presentation_id, first_row=4))

    assert issue == (4, "CATALOG_PERFUME_ARCHIVED")


async def test_stale_an_existing_perfume_that_disappeared() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    planned = _existing(rig, stored, presentation_id, first_row=5)
    del rig.world.perfumes.by_id[stored.id]

    assert await _stale_issue(rig, planned) == (5, "CATALOG_PERFUME_NOT_FOUND")


async def test_stale_the_identity_of_a_new_perfume_taken_by_someone_else() -> None:
    rig = _rig()
    _stored(rig, "Eros")  # same brand + name + concentration, another id

    issue = await _stale_issue(rig, _new(rig, 7, "Eros", first_row=6))

    assert issue == (6, "CATALOG_PERFUME_ALREADY_EXISTS")


async def test_stale_a_rename_that_collides_with_another_perfume() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig, "Luna")
    _stored(rig, "Sol")
    planned = _existing(rig, stored, presentation_id, first_row=3)
    planned = replace(planned, name="Sol")

    assert await _stale_issue(rig, planned) == (3, "CATALOG_PERFUME_ALREADY_EXISTS")


async def test_stale_a_presentation_that_disappeared() -> None:
    rig = _rig()
    stored, _ = _stored(rig)
    gone = UUID(int=777)

    issue = await _stale_issue(rig, _existing(rig, stored, gone, first_row=2))

    assert issue == (2, "CATALOG_PRESENTATION_NOT_FOUND")


async def test_stale_a_presentation_archived_since_the_validation() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    stored.presentations[0].is_active = False
    rig.world.seed(stored)

    issue = await _stale_issue(rig, _existing(rig, stored, presentation_id, first_row=2))

    assert issue == (2, "CATALOG_IMPORT_PRESENTATION_ARCHIVED")


async def test_stale_a_planned_new_presentation_whose_ml_is_now_taken() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig, ml=50)
    planned = _existing(
        rig,
        stored,
        presentation_id,
        presentations=(
            _presentation(50, 100_000, presentation_id=presentation_id),
            _presentation(100, 250_000, row=7),
        ),
    )
    # Someone added 100 ml from the panel meanwhile.
    add_presentation(stored, ml=100)
    rig.world.seed(stored)

    assert await _stale_issue(rig, planned) == (7, "CATALOG_PRESENTATION_ALREADY_EXISTS")


async def test_stale_a_fourth_photo_because_the_panel_added_one_meanwhile() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    for n in range(3):
        photo_id = UUID(int=500 + n)
        card, detail = photo_keys(stored.id, photo_id)
        unwrap_ok(stored.add_photo(photo_id, card, detail, 1200, 900, created_at=NOW))
    rig.world.seed(stored)
    planned = _existing(
        rig, stored, presentation_id, photos=(_photo(stored.id, URL_D, 4),), first_row=8
    )

    assert await _stale_issue(rig, planned) == (8, "CATALOG_PHOTO_LIMIT_REACHED")


async def test_stale_a_replaced_photo_that_was_removed_meanwhile() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    planned = _existing(
        rig,
        stored,
        presentation_id,
        photos=(_photo(stored.id, URL_D, 1, replaces=UUID(int=999)),),
        first_row=9,
    )

    assert await _stale_issue(rig, planned) == (9, "CATALOG_PHOTO_NOT_FOUND")


@pytest.mark.parametrize(
    ("archive", "code"),
    [
        ("brand", "CATALOG_PERFUME_BRAND_UNAVAILABLE"),
        ("family", "CATALOG_PERFUME_FAMILY_UNAVAILABLE"),
        ("concentration", "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"),
    ],
)
async def test_stale_a_reference_archived_since_the_validation_for_a_new_perfume(
    archive: str, code: str
) -> None:
    rig = _rig()
    getattr(rig.world, archive).is_active = False

    assert await _stale_issue(rig, _new(rig, 7, "Eros", first_row=3)) == (3, code)


async def test_stale_a_reference_that_no_longer_exists_for_a_new_perfume() -> None:
    rig = _rig()
    planned = _new(rig, 7, "Eros", brand_id=UUID(int=404))

    assert (await _stale_issue(rig, planned))[1] == "CATALOG_PERFUME_BRAND_UNAVAILABLE"


async def test_stale_a_changed_family_that_is_archived_for_an_existing_perfume() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    archived_family = make_family("Cítrica", active=False)
    rig.world.families.by_id[archived_family.id] = archived_family
    planned = _existing(rig, stored, presentation_id, family_id=archived_family.id, first_row=4)

    assert await _stale_issue(rig, planned) == (4, "CATALOG_PERFUME_FAMILY_UNAVAILABLE")


async def test_stale_a_changed_brand_or_concentration_archived_for_an_existing_perfume() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    other_brand = make_brand("Dior", active=False)
    other_concentration = make_concentration("Parfum", "PAR", active=False)
    rig.world.brands.by_id[other_brand.id] = other_brand
    rig.world.concentrations.by_id[other_concentration.id] = other_concentration
    brand_change = _existing(rig, stored, presentation_id, brand_id=other_brand.id, first_row=2)
    concentration_change = _existing(
        rig, stored, presentation_id, concentration_id=other_concentration.id, first_row=3
    )
    import_id = await _store(rig, brand_change, concentration_change)

    result = await rig.confirm.execute(import_id, ACTOR)

    assert _issues(result) == [
        (2, "CATALOG_PERFUME_BRAND_UNAVAILABLE"),
        (3, "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"),
    ]


async def test_stale_a_publish_refused_because_the_perfume_has_nothing_to_sell() -> None:
    rig = _rig()
    planned = _new(rig, 7, "Eros", presentations=(), publish=True, first_row=6)

    assert await _stale_issue(rig, planned) == (6, "CATALOG_PERFUME_NOTHING_TO_SELL")


async def test_stale_a_planned_value_that_no_longer_builds_becomes_an_issue_not_an_exception() -> (
    None
):
    rig = _rig()
    planned = _new(rig, 7, "", first_row=3)  # an empty name is refused by `PerfumeName`

    assert await _stale_issue(rig, planned) == (3, "CATALOG_PERFUME_NAME_INVALID")


async def test_stale_an_invalid_planned_presentation_is_reported_at_its_own_row() -> None:
    rig = _rig()
    planned = _new(rig, 7, "Eros", presentations=(_presentation(ml=0, row=12),), first_row=11)

    assert await _stale_issue(rig, planned) == (12, "CATALOG_PRESENTATION_ML_INVALID")


# --- confirm: all issues are collected, nothing is saved --------------------------------------


async def test_every_stale_row_is_reported_together_in_plan_order_one_issue_per_perfume() -> None:
    rig = _rig()
    archived, archived_presentation = _stored(rig, "Uno")
    archived.archive()
    rig.world.seed(archived)
    gone, gone_presentation = _stored(rig, "Dos")
    taken, _ = _stored(rig, "Tres")
    del rig.world.perfumes.by_id[gone.id]
    import_id = await _store(
        rig,
        _existing(rig, archived, archived_presentation, first_row=2),
        _new(rig, 7, "Valido", first_row=3),
        _existing(rig, gone, gone_presentation, first_row=4),
        _new(rig, 8, "Tres", first_row=5),  # identity taken by `taken`
        # Two refusals in one perfume: only its first one is reported.
        _new(rig, 9, "Otro", presentations=(), photos=(), publish=True, first_row=6),
    )

    result = await rig.confirm.execute(import_id, ACTOR)

    assert error_code(result) == "CATALOG_IMPORT_STALE"
    assert _issues(result) == [
        (2, "CATALOG_PERFUME_ARCHIVED"),
        (4, "CATALOG_PERFUME_NOT_FOUND"),
        (5, "CATALOG_PERFUME_ALREADY_EXISTS"),
        (6, "CATALOG_PERFUME_NOTHING_TO_SELL"),
    ]
    assert taken.id in rig.world.perfumes.by_id


async def test_an_issue_carries_row_column_code_and_message() -> None:
    rig = _rig()
    stored, presentation_id = _stored(rig)
    stored.archive()
    rig.world.seed(stored)
    result = await rig.confirm.execute(
        await _store(rig, _existing(rig, stored, presentation_id, first_row=4)), ACTOR
    )

    details = error_details(result)

    assert details == {
        "issues": [
            {
                "row": 4,
                "column": None,
                "code": "CATALOG_PERFUME_ARCHIVED",
                "message": "An archived perfume is read-only; restore it first",
            }
        ]
    }


async def test_a_stale_confirm_saves_nothing_even_though_valid_perfumes_were_written_first() -> (
    None
):
    rig = _rig()
    stored, presentation_id = _stored(rig, "Luna")
    other, other_presentation = _stored(rig, "Sol")
    other.archive()
    rig.world.seed(other)
    import_id = await _store(
        rig,
        _new(rig, 7, "Nuevo", first_row=2),  # valid, written inside the transaction
        _existing(rig, stored, presentation_id, first_row=3),  # valid, price changes
        _existing(rig, other, other_presentation, first_row=4),  # stale in the middle...
        _new(rig, 8, "Otro Nuevo", first_row=5),  # ...and one more valid after it
    )
    before = _snapshot(rig)

    result = await rig.confirm.execute(import_id, ACTOR)

    assert _issues(result) == [(4, "CATALOG_PERFUME_ARCHIVED")]
    written = rig.transactions.before_rollback["perfumes"]
    assert UUID(int=7) in written and UUID(int=8) in written  # they were written...
    assert written[stored.id].presentations[0].price.amount.cents == 199_900
    assert rig.transactions.rolled_back is True
    assert _snapshot(rig) == before  # ...and the rollback undid every one of them
    assert UUID(int=7) not in rig.world.perfumes.by_id
    assert rig.world.perfumes.by_id[stored.id].presentations[0].price.amount.cents == 250_000


async def test_a_stale_confirm_leaves_the_import_ready_with_its_plan_and_deletes_no_objects() -> (
    None
):
    rig = _rig()
    stored, presentation_id = _stored(rig)
    stored.archive()
    rig.world.seed(stored)
    planned = _existing(
        rig, stored, presentation_id, photos=(_photo(stored.id, URL_D, 1, replaces=UUID(int=9)),)
    )
    import_id = await _store(rig, _new(rig, 7, "Nuevo"), planned)

    unwrap_ok_or_stale = await rig.confirm.execute(import_id, ACTOR)

    assert error_code(unwrap_ok_or_stale) == "CATALOG_IMPORT_STALE"
    item = rig.imports.by_id[import_id]
    assert item.status is ImportStatus.READY
    assert item.plan is not None and len(item.plan.perfumes) == 2
    assert (item.applied_at, item.applied_by) == (None, None)
    assert rig.storage.deleted == []  # neither the replaced photo's nor the staged objects


async def test_an_unexpected_exception_rolls_back_too_and_deletes_no_objects() -> None:
    rig = _rig()
    import_id = await _store(
        rig, _new(rig, 7, "Uno", first_row=2), _new(rig, 8, "Dos", first_row=3)
    )
    before = _snapshot(rig)
    real_add = rig.world.perfumes.add
    calls = 0

    async def exploding_add(perfume: Perfume) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database down")
        return await real_add(perfume)

    rig.world.perfumes.add = exploding_add  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await rig.confirm.execute(import_id, ACTOR)

    assert _snapshot(rig) == before
    assert rig.storage.deleted == []


# --- discard ----------------------------------------------------------------------------------


async def test_discarding_a_ready_import_marks_it_discarded_and_deletes_the_staged_keys() -> None:
    rig = _rig()
    planned = _new(
        rig, 7, "Eros", photos=(_photo(UUID(int=7), URL_A, 1), _photo(UUID(int=7), URL_B, 2))
    )
    import_id = await _store(rig, planned)
    keys = ImportPlan((planned,)).staged_keys()
    rig.storage.objects.update({key: b"x" for key in keys})

    unwrap_ok(await rig.discard.execute(import_id))

    item = rig.imports.by_id[import_id]
    assert item.status is ImportStatus.DISCARDED
    assert (item.discarded_at, item.plan) == (NOW, None)
    assert rig.storage.deleted == keys
    assert rig.storage.objects == {}
    assert rig.world.perfumes.by_id == {}  # discarding never touches the catalog


async def test_discard_deletes_the_objects_after_the_commit() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"))

    unwrap_ok(await rig.discard.execute(import_id))

    assert rig.transactions.deleted_at_end == []
    assert len(rig.storage.deleted) == 2


async def test_discarding_an_unknown_import_is_not_found() -> None:
    rig = _rig()

    assert error_code(await rig.discard.execute(UUID(int=404))) == "CATALOG_IMPORT_NOT_FOUND"


@pytest.mark.parametrize(
    "status",
    [
        ImportStatus.PENDING,
        ImportStatus.VALIDATING,
        ImportStatus.INVALID,
        ImportStatus.FAILED,
        ImportStatus.EXPIRED,
        ImportStatus.APPLIED,
        ImportStatus.DISCARDED,
    ],
)
async def test_discarding_an_import_that_is_not_ready_is_refused_and_deletes_nothing(
    status: ImportStatus,
) -> None:
    rig = _rig()
    item = _with_status(_ready(ImportPlan((_new(rig, 7, "Eros"),))), status)
    await rig.imports.add(item)
    before = _snapshot(rig)

    result = await rig.discard.execute(item.id)

    assert error_code(result) == "CATALOG_IMPORT_NOT_READY"
    assert _snapshot(rig) == before
    assert rig.storage.deleted == []


async def test_a_ready_import_already_past_expires_at_can_still_be_discarded() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"), expires_at=NOW - timedelta(hours=1))

    unwrap_ok(await rig.discard.execute(import_id))

    assert rig.imports.by_id[import_id].status is ImportStatus.DISCARDED
    assert len(rig.storage.deleted) == 2


async def test_discarding_twice_the_second_is_not_ready() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"))
    unwrap_ok(await rig.discard.execute(import_id))

    assert error_code(await rig.discard.execute(import_id)) == "CATALOG_IMPORT_NOT_READY"
    assert len(rig.storage.deleted) == 2  # not deleted twice


async def test_a_failure_deleting_the_staged_objects_does_not_undo_the_discard() -> None:
    rig = _rig(fail_deletes=True)
    import_id = await _store(rig, _new(rig, 7, "Eros"))

    with structlog.testing.capture_logs() as logs:
        unwrap_ok(await rig.discard.execute(import_id))

    assert rig.imports.by_id[import_id].status is ImportStatus.DISCARDED
    assert [log["event"] for log in logs] == ["catalog_import_objects_not_deleted"]


async def test_a_confirmed_import_cannot_be_discarded_and_its_photos_stay() -> None:
    rig = _rig()
    import_id = await _store(rig, _new(rig, 7, "Eros"))
    unwrap_ok(await rig.confirm.execute(import_id, ACTOR))

    result = await rig.discard.execute(import_id)

    assert error_code(result) == "CATALOG_IMPORT_NOT_READY"
    assert rig.storage.deleted == []
    assert len(rig.world.perfumes.by_id[UUID(int=7)].photos) == 1


# Silence "unused" for helpers kept for readability of failures.
_ = (Ok,)
