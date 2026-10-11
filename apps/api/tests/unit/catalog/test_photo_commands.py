"""Plan 006: add, remove and reorder photos (use cases over in-memory adapters and fakes)."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
import structlog.testing

from fragancia_api.modules.catalog.application.commands.photos import (
    AddPhoto,
    RemovePhoto,
    ReorderPhotos,
)
from fragancia_api.modules.catalog.application.ports import ProcessedImage
from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PhotoFormatInvalid,
    PhotoTooSmall,
)
from fragancia_api.modules.catalog.domain.perfume import Perfume, photo_keys
from fragancia_api.modules.catalog.infrastructure.in_memory_media import (
    FakeImageProcessor,
    InMemoryMediaStorage,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_perfumes import InMemoryPerfumes
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, Result
from tests.unit.catalog.perfume_support import (
    World,
    error_code,
    error_details,
    make_perfume,
    unwrap_ok,
)

UNKNOWN_ID = UUID(int=404)
CLOCK = FixedClock()
IMAGE = ProcessedImage(card=b"card-bytes", detail=b"detail-bytes", width=1200, height=900)


class _CommitSnapshot(InMemoryTransactionRunner):
    """Records what the storage had deleted when the transaction finished (the commit point)."""

    def __init__(self, storage: InMemoryMediaStorage) -> None:
        self._storage = storage
        self.deleted_at_commit: list[str] | None = None

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        result = await work()
        self.deleted_at_commit = list(self._storage.deleted)
        return result


class _TransactionExplodes(InMemoryTransactionRunner):
    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        raise RuntimeError("database went away")


class _SaveConflicts(InMemoryPerfumes):
    async def save(self, perfume: Perfume) -> Result[None, PerfumeAlreadyExists]:
        return Err(PerfumeAlreadyExists())


def _world_with_perfume() -> tuple[World, Perfume]:
    world = World()
    return world, world.seed(make_perfume(world.brand, world.family, world.concentration))


def _add_command(
    world: World,
    storage: InMemoryMediaStorage,
    images: FakeImageProcessor | None = None,
    *,
    perfumes: InMemoryPerfumes | None = None,
    transactions: InMemoryTransactionRunner | None = None,
) -> AddPhoto:
    return AddPhoto(
        perfumes=perfumes or world.perfumes,
        images=images or FakeImageProcessor(IMAGE),
        storage=storage,
        transactions=transactions or InMemoryTransactionRunner(),
        clock=CLOCK,
    )


async def _upload(world: World, perfume: Perfume, storage: InMemoryMediaStorage) -> UUID:
    return unwrap_ok(await _add_command(world, storage).execute(perfume.id, b"raw"))


def _stored(world: World, perfume_id: UUID) -> Perfume:
    return world.perfumes.by_id[perfume_id]


# --- AddPhoto ---------------------------------------------------------------------------------


async def test_adds_the_photo_uploads_both_renditions_and_saves_it_with_the_clock() -> None:
    world, perfume = _world_with_perfume()
    storage = InMemoryMediaStorage()
    images = FakeImageProcessor(IMAGE)

    result = await _add_command(world, storage, images).execute(perfume.id, b"raw-upload")

    photo_id = unwrap_ok(result)
    card_key, detail_key = photo_keys(perfume.id, photo_id)
    assert images.calls == [b"raw-upload"]
    assert storage.objects == {card_key: b"card-bytes", detail_key: b"detail-bytes"}
    assert storage.content_types == {card_key: "image/webp", detail_key: "image/webp"}
    [photo] = _stored(world, perfume.id).photos
    assert (photo.id, photo.position, photo.card_key, photo.detail_key) == (
        photo_id,
        1,
        card_key,
        detail_key,
    )
    assert (photo.width, photo.height, photo.created_at) == (1200, 900, CLOCK.now())
    assert storage.deleted == []


async def test_each_upload_gets_a_fresh_id_and_the_next_position() -> None:
    world, perfume = _world_with_perfume()
    storage = InMemoryMediaStorage()

    first = await _upload(world, perfume, storage)
    second = await _upload(world, perfume, storage)

    assert first != second
    assert [(p.id, p.position) for p in _stored(world, perfume.id).photos] == [
        (first, 1),
        (second, 2),
    ]
    assert len(storage.objects) == 4


@pytest.mark.parametrize(
    "error",
    [
        PhotoFormatInvalid(details={"formats": ["jpeg", "png", "webp"]}),
        PhotoTooSmall(details={"min_side": 600}),
    ],
)
async def test_a_processor_error_passes_through_without_uploading_or_touching_the_perfume(
    error: PhotoFormatInvalid | PhotoTooSmall,
) -> None:
    world, perfume = _world_with_perfume()
    storage = InMemoryMediaStorage()

    result = await _add_command(world, storage, FakeImageProcessor(error=error)).execute(
        perfume.id, b"raw"
    )

    assert isinstance(result, Err)
    assert result.error is error
    assert storage.objects == {}
    assert storage.deleted == []
    assert _stored(world, perfume.id).photos == []


async def test_an_unknown_perfume_is_not_found_and_the_uploaded_objects_are_deleted() -> None:
    world = World()
    storage = InMemoryMediaStorage()

    result = await _add_command(world, storage).execute(UNKNOWN_ID, b"raw")

    assert error_code(result) == "CATALOG_PERFUME_NOT_FOUND"
    assert storage.objects == {}
    assert len(storage.deleted) == 2


async def test_the_fourth_photo_is_refused_and_only_its_own_objects_are_deleted() -> None:
    world, perfume = _world_with_perfume()
    storage = InMemoryMediaStorage()
    for _ in range(3):
        await _upload(world, perfume, storage)
    kept = dict(storage.objects)

    result = await _add_command(world, storage).execute(perfume.id, b"raw")

    assert error_code(result) == "CATALOG_PHOTO_LIMIT_REACHED"
    assert error_details(result) == {"max": 3}
    assert storage.objects == kept
    assert len(storage.deleted) == 2
    assert set(storage.deleted).isdisjoint(kept)
    assert len(_stored(world, perfume.id).photos) == 3


async def test_an_archived_perfume_refuses_the_photo_and_the_objects_are_deleted() -> None:
    world, perfume = _world_with_perfume()
    perfume.archive()
    storage = InMemoryMediaStorage()

    result = await _add_command(world, storage).execute(perfume.id, b"raw")

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert storage.objects == {}
    assert len(storage.deleted) == 2


async def test_a_failing_compensation_delete_does_not_mask_the_domain_error() -> None:
    world = World()
    storage = InMemoryMediaStorage(fail_deletes=True)

    result = await _add_command(world, storage).execute(UNKNOWN_ID, b"raw")

    assert error_code(result) == "CATALOG_PERFUME_NOT_FOUND"
    assert len(storage.objects) == 2  # orphans left behind, only logged


async def test_a_failed_compensation_delete_is_logged_with_the_keys() -> None:
    world = World()
    storage = InMemoryMediaStorage(fail_deletes=True)

    with structlog.testing.capture_logs() as logs:
        await _add_command(world, storage).execute(UNKNOWN_ID, b"raw")

    [entry] = [log for log in logs if log["event"] == "catalog_photo_objects_not_deleted"]
    assert sorted(entry["keys"]) == sorted(storage.objects)
    assert entry["log_level"] == "warning"


async def test_a_save_conflict_is_returned_and_compensated() -> None:
    world, perfume = _world_with_perfume()
    conflicting = _SaveConflicts(world.brands, world.families, world.concentrations, perfume)
    storage = InMemoryMediaStorage()

    result = await _add_command(world, storage, perfumes=conflicting).execute(perfume.id, b"raw")

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"
    assert storage.objects == {}
    assert len(storage.deleted) == 2


async def test_a_failing_upload_deletes_what_was_put_and_raises() -> None:
    world, perfume = _world_with_perfume()

    class _SecondPutFails(InMemoryMediaStorage):
        async def put(self, key: str, data: bytes, content_type: str) -> None:
            if self.objects:
                raise RuntimeError("storage put failed")
            await super().put(key, data, content_type)

    storage = _SecondPutFails()

    with pytest.raises(RuntimeError, match="storage put failed"):
        await _add_command(world, storage).execute(perfume.id, b"raw")

    assert storage.objects == {}
    assert len(storage.deleted) == 2  # both keys are requested; a missing one is not an error
    assert _stored(world, perfume.id).photos == []


async def test_an_exception_in_the_transaction_deletes_the_objects_and_propagates() -> None:
    world, perfume = _world_with_perfume()
    storage = InMemoryMediaStorage()

    with pytest.raises(RuntimeError, match="database went away"):
        await _add_command(world, storage, transactions=_TransactionExplodes()).execute(
            perfume.id, b"raw"
        )

    assert storage.objects == {}
    assert len(storage.deleted) == 2


# --- RemovePhoto ------------------------------------------------------------------------------


def _remove_command(
    world: World,
    storage: InMemoryMediaStorage,
    transactions: InMemoryTransactionRunner | None = None,
) -> RemovePhoto:
    return RemovePhoto(
        perfumes=world.perfumes,
        storage=storage,
        transactions=transactions or InMemoryTransactionRunner(),
    )


async def _perfume_with_photos(
    count: int, storage: InMemoryMediaStorage
) -> tuple[World, Perfume, list[UUID]]:
    world, perfume = _world_with_perfume()
    ids = [await _upload(world, perfume, storage) for _ in range(count)]
    return world, perfume, ids


async def test_removes_the_photo_renumbers_the_rest_and_deletes_both_objects() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (first, second, third) = await _perfume_with_photos(3, storage)

    result = await _remove_command(world, storage).execute(perfume.id, second)

    assert isinstance(result, Ok)
    assert [(p.id, p.position) for p in _stored(world, perfume.id).photos] == [
        (first, 1),
        (third, 2),
    ]
    assert sorted(storage.deleted) == sorted(photo_keys(perfume.id, second))
    assert set(storage.objects) == {
        *photo_keys(perfume.id, first),
        *photo_keys(perfume.id, third),
    }


async def test_the_objects_are_deleted_only_after_the_transaction_finished() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (photo_id,) = await _perfume_with_photos(1, storage)
    transactions = _CommitSnapshot(storage)

    await _remove_command(world, storage, transactions).execute(perfume.id, photo_id)

    assert transactions.deleted_at_commit == []
    assert len(storage.deleted) == 2


async def test_a_failing_object_delete_still_removes_the_photo_and_is_logged() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (photo_id,) = await _perfume_with_photos(1, storage)
    storage.fail_deletes = True

    with structlog.testing.capture_logs() as logs:
        result = await _remove_command(world, storage).execute(perfume.id, photo_id)

    assert isinstance(result, Ok)
    assert _stored(world, perfume.id).photos == []
    assert len(storage.objects) == 2  # the orphans stay in the bucket
    assert [log["event"] for log in logs] == ["catalog_photo_objects_not_deleted"]


@pytest.mark.parametrize(
    ("perfume_known", "photo_known", "code"),
    [
        (False, False, "CATALOG_PERFUME_NOT_FOUND"),
        (True, False, "CATALOG_PHOTO_NOT_FOUND"),
    ],
)
async def test_unknown_perfume_or_photo_is_not_found_and_deletes_nothing(
    perfume_known: bool, photo_known: bool, code: str
) -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (photo_id,) = await _perfume_with_photos(1, storage)
    target = perfume.id if perfume_known else UNKNOWN_ID
    photo = photo_id if photo_known else UNKNOWN_ID

    result = await _remove_command(world, storage).execute(target, photo)

    assert error_code(result) == code
    assert storage.deleted == []
    assert len(_stored(world, perfume.id).photos) == 1


async def test_an_archived_perfume_refuses_the_removal_and_deletes_nothing() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (photo_id,) = await _perfume_with_photos(1, storage)
    _stored(world, perfume.id).archive()

    result = await _remove_command(world, storage).execute(perfume.id, photo_id)

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert storage.deleted == []
    assert len(_stored(world, perfume.id).photos) == 1


# --- ReorderPhotos ----------------------------------------------------------------------------


def _reorder_command(world: World) -> ReorderPhotos:
    return ReorderPhotos(perfumes=world.perfumes, transactions=InMemoryTransactionRunner())


async def test_reorders_the_photos_and_saves_the_positions() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (first, second, third) = await _perfume_with_photos(3, storage)

    result = await _reorder_command(world).execute(perfume.id, [third, second, first])

    assert isinstance(result, Ok)
    assert [(p.id, p.position) for p in _stored(world, perfume.id).photos] == [
        (third, 1),
        (second, 2),
        (first, 3),
    ]
    assert storage.deleted == []


async def test_rejects_an_order_that_is_not_the_current_set_and_keeps_the_stored_one() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (first, second) = await _perfume_with_photos(2, storage)

    result = await _reorder_command(world).execute(perfume.id, [first, UNKNOWN_ID])

    assert error_code(result) == "CATALOG_PHOTO_ORDER_INVALID"
    assert [p.id for p in _stored(world, perfume.id).photos] == [first, second]


async def test_reordering_an_unknown_perfume_is_not_found() -> None:
    result = await _reorder_command(World()).execute(UNKNOWN_ID, [])

    assert error_code(result) == "CATALOG_PERFUME_NOT_FOUND"


async def test_reordering_an_archived_perfume_is_refused() -> None:
    storage = InMemoryMediaStorage()
    world, perfume, (first, second) = await _perfume_with_photos(2, storage)
    _stored(world, perfume.id).archive()

    result = await _reorder_command(world).execute(perfume.id, [second, first])

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert [p.id for p in _stored(world, perfume.id).photos] == [first, second]
