"""Plan 008, domain layer: `CatalogImport.apply` / `discard` / `is_confirmable`, the two new
errors, and the `Perfume` additions the confirm relies on (`create` with an id, `add_photo` with
`source_url`, `replace_photo`)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    CatalogImportApplied,
    ImportIssue,
    ImportStatus,
    ImportSummary,
)
from fragancia_api.modules.catalog.domain.errors import (
    ImportNotReady,
    ImportStale,
    ImportStockMadeToOrder,
    PerfumeArchived,
    PhotoNotFound,
)
from fragancia_api.modules.catalog.domain.perfume import (
    PHOTOS_MAX,
    Description,
    Gender,
    Notes,
    Perfume,
    PerfumeName,
    Photo,
    photo_keys,
)
from fragancia_api.shared.kernel import ConflictError, InvalidValueError, Result, new_id
from tests.unit.catalog.import_support import PLAN
from tests.unit.catalog.perfume_support import (
    LATER,
    NOW,
    error_code,
    make_brand,
    make_concentration,
    make_family,
    make_perfume,
    unwrap_ok,
)

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=60)
EXPIRES_AT = T0 + timedelta(minutes=2) + TTL
UNKNOWN = UUID(int=404)


def _ready() -> CatalogImport:
    item = CatalogImport.create(b"PK\x03\x04data", "admin-1", T0)
    item.start(T0 + timedelta(minutes=1))
    item.finish_ready(T0 + timedelta(minutes=2), TTL, 3, ImportSummary(1, 0, 1, 0, 3), PLAN)
    return item


def _invalid() -> CatalogImport:
    item = CatalogImport.create(b"PK\x03\x04data", "admin-1", T0)
    item.start(T0)
    item.finish_invalid(T0, 1, [])
    return item


def _not_ready_imports() -> list[CatalogImport]:
    pending = CatalogImport.create(b"PK\x03\x04data", "admin-1", T0)
    validating = CatalogImport.create(b"PK\x03\x04data", "admin-1", T0)
    validating.start(T0)
    failed = CatalogImport.create(b"PK\x03\x04data", "admin-1", T0)
    failed.fail(T0, ImportIssue(None, None, "CATALOG_IMPORT_FAILED", "x"))
    expired = _ready()
    expired.expire()
    applied = _ready()
    applied.apply(T0, "admin-1", {})
    discarded = _ready()
    discarded.discard(T0)
    return [pending, validating, _invalid(), failed, expired, applied, discarded]


# --- CatalogImport.apply ----------------------------------------------------------------------


def test_apply_marks_a_ready_import_applied_with_who_and_when_and_drops_the_plan() -> None:
    item = _ready()

    item.apply(T0 + timedelta(minutes=5), "admin-7", {})

    assert item.status is ImportStatus.APPLIED
    assert (item.applied_at, item.applied_by) == (T0 + timedelta(minutes=5), "admin-7")
    assert item.plan is None
    assert item.discarded_at is None


@pytest.mark.parametrize("item", _not_ready_imports(), ids=lambda i: i.status.value)
def test_apply_raises_from_any_status_but_ready(item: CatalogImport) -> None:
    with pytest.raises(ValueError):
        item.apply(T0, "admin-1", {})


def test_apply_records_one_import_applied_event_with_the_counts_and_the_actor() -> None:
    item = _ready()
    counts = {UUID(int=1): 10, UUID(int=2): 0}

    item.apply(T0, "admin-7", counts)

    [event] = item.pull_events()
    assert isinstance(event, CatalogImportApplied)
    assert (event.import_id, event.actor_id, event.stock_counts) == (item.id, "admin-7", counts)
    assert item.pull_events() == []


def test_the_event_copies_the_counts_so_a_later_change_of_the_mapping_does_not_leak() -> None:
    item = _ready()
    counts = {UUID(int=1): 10}

    item.apply(T0, "admin-7", counts)
    counts[UUID(int=2)] = 99

    [event] = item.pull_events()
    assert isinstance(event, CatalogImportApplied)
    assert event.stock_counts == {UUID(int=1): 10}


def test_the_event_name_is_a_string_for_inventory_and_its_payload_has_string_keys() -> None:
    item = _ready()
    item.apply(T0, "admin-7", {UUID(int=1): 10, UUID(int=2): 0})

    [event] = item.pull_events()

    assert CatalogImportApplied.name == "catalog.import.applied"
    assert event.payload() == {
        "import_id": str(item.id),
        "actor_id": "admin-7",
        "stock_counts": {str(UUID(int=1)): 10, str(UUID(int=2)): 0},
    }


def test_an_import_without_counts_still_records_the_event_with_an_empty_map() -> None:
    item = _ready()

    item.apply(T0, "admin-7", {})

    [event] = item.pull_events()
    assert event.payload()["stock_counts"] == {}


@pytest.mark.parametrize("item", _not_ready_imports(), ids=lambda i: i.status.value)
def test_apply_from_a_status_but_ready_records_no_event(item: CatalogImport) -> None:
    item.pull_events()

    with pytest.raises(ValueError):
        item.apply(T0, "admin-1", {UUID(int=1): 1})

    assert item.pull_events() == []


# --- CatalogImport.discard --------------------------------------------------------------------


def test_discard_marks_a_ready_import_discarded_returns_the_staged_keys_and_drops_the_plan() -> (
    None
):
    item = _ready()
    expected = PLAN.staged_keys()

    keys = item.discard(T0 + timedelta(minutes=5))

    assert keys == expected
    assert len(keys) == 6
    assert item.status is ImportStatus.DISCARDED
    assert item.discarded_at == T0 + timedelta(minutes=5)
    assert (item.plan, item.applied_at, item.applied_by) == (None, None, None)


@pytest.mark.parametrize("item", _not_ready_imports(), ids=lambda i: i.status.value)
def test_discard_raises_from_any_status_but_ready(item: CatalogImport) -> None:
    with pytest.raises(ValueError):
        item.discard(T0)


def test_a_ready_import_past_its_expiry_can_still_be_discarded() -> None:
    item = _ready()

    keys = item.discard(EXPIRES_AT + timedelta(hours=1))

    assert item.status is ImportStatus.DISCARDED
    assert len(keys) == 6


# --- CatalogImport.is_confirmable -------------------------------------------------------------


def test_a_ready_import_is_confirmable_until_the_instant_before_expires_at() -> None:
    item = _ready()

    assert item.expires_at == EXPIRES_AT
    assert item.is_confirmable(EXPIRES_AT - timedelta(microseconds=1)) is True
    assert item.is_confirmable(T0) is True


def test_a_ready_import_is_not_confirmable_at_expires_at_exactly_nor_after() -> None:
    item = _ready()

    assert item.is_confirmable(EXPIRES_AT) is False
    assert item.is_confirmable(EXPIRES_AT + timedelta(seconds=1)) is False


@pytest.mark.parametrize("item", _not_ready_imports(), ids=lambda i: i.status.value)
def test_an_import_that_is_not_ready_is_never_confirmable(item: CatalogImport) -> None:
    assert item.is_confirmable(T0) is False


# --- errors -----------------------------------------------------------------------------------


def test_not_ready_and_stale_are_conflicts_with_their_codes() -> None:
    not_ready, stale = ImportNotReady(), ImportStale(details={"issues": []})

    assert isinstance(not_ready, ConflictError)
    assert isinstance(stale, ConflictError)
    assert not_ready.code == "CATALOG_IMPORT_NOT_READY"
    assert stale.code == "CATALOG_IMPORT_STALE"
    assert stale.details == {"issues": []}


def test_a_count_on_a_made_to_order_row_is_an_invalid_value_with_its_own_code() -> None:
    error = ImportStockMadeToOrder()

    assert isinstance(error, InvalidValueError)
    assert error.code == "CATALOG_IMPORT_STOCK_MADE_TO_ORDER"


# --- Perfume.create with an id ----------------------------------------------------------------


def _bare(**extra: object) -> Perfume:
    brand, family, concentration = make_brand(), make_family(), make_concentration()
    return Perfume.create(
        brand_id=brand.id,
        concentration_id=concentration.id,
        family_id=family.id,
        name=unwrap_ok(PerfumeName.create("Eros")),
        slug="eros",
        gender=Gender.MEN,
        description=unwrap_ok(Description.create("")),
        notes=unwrap_ok(Notes.create([], [], [])),
        now=NOW,
        **extra,  # type: ignore[arg-type]
    )


def test_create_keeps_the_id_it_is_given() -> None:
    assigned = new_id()

    assert _bare(id=assigned).id == assigned


def test_create_without_an_id_generates_a_distinct_one() -> None:
    assert _bare().id != _bare().id


# --- Perfume.add_photo with source_url --------------------------------------------------------


def _perfume() -> Perfume:
    return make_perfume(make_brand(), make_family(), make_concentration())


def _add(perfume: Perfume, *, source_url: str | None = None) -> UUID:
    photo_id = new_id()
    card, detail = photo_keys(perfume.id, photo_id)
    unwrap_ok(
        perfume.add_photo(photo_id, card, detail, 1200, 900, created_at=NOW, source_url=source_url)
    )
    return photo_id


def _with(count: int) -> tuple[Perfume, list[UUID]]:
    perfume = _perfume()
    return perfume, [_add(perfume, source_url=f"https://img.test/{n}.jpg") for n in range(count)]


def test_add_photo_stores_the_source_url() -> None:
    perfume = _perfume()

    _add(perfume, source_url="https://img.test/a.jpg")

    assert [p.source_url for p in perfume.photos] == ["https://img.test/a.jpg"]


def test_add_photo_without_a_source_url_leaves_it_none_as_the_panel_does() -> None:
    perfume = _perfume()

    _add(perfume)

    assert perfume.photos[0].source_url is None


# --- Perfume.replace_photo --------------------------------------------------------------------


def _replace(
    perfume: Perfume, old: UUID, *, source_url: str | None = "https://img.test/new.jpg"
) -> tuple[UUID, tuple[str, str], Result[Photo, PerfumeArchived | PhotoNotFound]]:
    photo_id = new_id()
    card, detail = photo_keys(perfume.id, photo_id)
    result = perfume.replace_photo(
        old, photo_id, card, detail, 800, 1000, source_url=source_url, created_at=LATER
    )
    return photo_id, (card, detail), result


def test_replace_photo_2_of_3_keeps_positions_1_to_3_without_gaps_and_the_slot() -> None:
    perfume, (first, second, third) = _with(3)

    new_id_, _, result = _replace(perfume, second)

    unwrap_ok(result)
    assert [p.id for p in perfume.photos] == [first, new_id_, third]
    assert [p.position for p in perfume.photos] == [1, 2, 3]


def test_replace_photo_stores_the_new_photo_data_and_returns_the_old_photo() -> None:
    perfume, (first, second, _third) = _with(3)
    old = next(p for p in perfume.photos if p.id == second)

    new_photo_id, keys, result = _replace(perfume, second, source_url="https://img.test/new.jpg")

    returned = unwrap_ok(result)
    assert returned.id == second
    assert (returned.card_key, returned.detail_key) == (old.card_key, old.detail_key)
    replaced = perfume.photos[1]
    assert replaced.id == new_photo_id
    assert (replaced.card_key, replaced.detail_key) == keys
    assert (replaced.width, replaced.height) == (800, 1000)
    assert replaced.source_url == "https://img.test/new.jpg"
    assert replaced.created_at == LATER
    assert first == perfume.photos[0].id


def test_replace_photo_records_only_the_old_id_as_removed() -> None:
    perfume, (_first, second, _third) = _with(3)

    _replace(perfume, second)

    assert perfume.removed_photo_ids == [second]


def test_replace_photo_works_on_a_full_perfume_without_hitting_the_limit() -> None:
    perfume, ids = _with(PHOTOS_MAX)

    _, _, result = _replace(perfume, ids[0])

    unwrap_ok(result)
    assert len(perfume.photos) == PHOTOS_MAX


def test_replace_photo_refuses_an_unknown_old_photo_and_changes_nothing() -> None:
    perfume, ids = _with(2)

    _, _, result = _replace(perfume, UNKNOWN)

    assert error_code(result) == "CATALOG_PHOTO_NOT_FOUND"
    assert [p.id for p in perfume.photos] == ids
    assert perfume.removed_photo_ids == []


def test_replace_photo_refuses_an_archived_perfume_and_changes_nothing() -> None:
    perfume, ids = _with(2)
    perfume.archive()

    _, _, result = _replace(perfume, ids[0])

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert [p.id for p in perfume.photos] == ids
    assert perfume.removed_photo_ids == []
