"""Commands on the photos of a perfume: add, remove and reorder.

Photos are saved with their perfume, whose row is locked for the whole transaction. Object
storage is not transactional, so an upload happens before the transaction (and is compensated
when it refuses) and a removal deletes its objects after the commit.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog

from fragancia_api.modules.catalog.application.ports import ImageProcessor, MediaStorage
from fragancia_api.modules.catalog.domain.errors import PerfumeNotFound
from fragancia_api.modules.catalog.domain.perfume import photo_keys
from fragancia_api.modules.catalog.domain.repositories import PerfumeRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PhotoUploadLimit:
    """The largest accepted upload, for the HTTP layer to enforce before reading it all."""

    max_bytes: int


async def _delete_quietly(storage: MediaStorage, keys: Sequence[str]) -> None:
    """Delete objects, logging (never raising) a failure: a leftover object is an orphan."""
    try:
        await storage.delete(keys)
    except Exception:
        log.warning("catalog_photo_objects_not_deleted", keys=list(keys), exc_info=True)


class AddPhoto:
    """Command: process an image and add it as the next photo of a perfume.

    The size limit of the upload is checked by the caller, before the bytes reach this command.
    """

    def __init__(
        self,
        *,
        perfumes: PerfumeRepository,
        images: ImageProcessor,
        storage: MediaStorage,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._perfumes = perfumes
        self._images = images
        self._storage = storage
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID, data: bytes) -> Result[UUID, DomainError]:
        match await self._images.process(data):
            case Err(invalid):
                return Err(invalid)
            case Ok(image):
                pass
        photo_id = new_id()
        card_key, detail_key = photo_keys(perfume_id, photo_id)
        try:
            await self._storage.put(card_key, image.card, "image/webp")
            await self._storage.put(detail_key, image.detail, "image/webp")
        except Exception:
            await _delete_quietly(self._storage, [card_key, detail_key])
            raise

        async def work() -> Result[UUID, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.add_photo(
                photo_id,
                card_key,
                detail_key,
                image.width,
                image.height,
                created_at=self._clock.now(),
            ):
                case Err(refused):
                    return Err(refused)
                case Ok():
                    pass
            match await self._perfumes.save(perfume):
                case Err(conflict):
                    return Err(conflict)
                case Ok():
                    return Ok(photo_id)

        try:
            result = await self._transactions.run(work)
        except Exception:
            await _delete_quietly(self._storage, [card_key, detail_key])
            raise
        if isinstance(result, Err):
            await _delete_quietly(self._storage, [card_key, detail_key])
        return result


class RemovePhoto:
    """Command: remove a photo, renumber the rest, then delete its objects after the commit."""

    def __init__(
        self,
        *,
        perfumes: PerfumeRepository,
        storage: MediaStorage,
        transactions: TransactionRunner,
    ) -> None:
        self._perfumes = perfumes
        self._storage = storage
        self._transactions = transactions

    async def execute(self, perfume_id: UUID, photo_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[list[str], DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.remove_photo(photo_id):
                case Err(refused):
                    return Err(refused)
                case Ok(photo):
                    pass
            match await self._perfumes.save(perfume):
                case Err(conflict):
                    return Err(conflict)
                case Ok():
                    return Ok([photo.card_key, photo.detail_key])

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok(keys):
                await _delete_quietly(self._storage, keys)
                return Ok(None)


class ReorderPhotos:
    """Command: set the order of the photos of a perfume (the full list of their ids)."""

    def __init__(self, *, perfumes: PerfumeRepository, transactions: TransactionRunner) -> None:
        self._perfumes = perfumes
        self._transactions = transactions

    async def execute(self, perfume_id: UUID, photo_ids: list[UUID]) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.reorder_photos(photo_ids):
                case Err(refused):
                    return Err(refused)
                case Ok():
                    pass
            match await self._perfumes.save(perfume):
                case Err(conflict):
                    return Err(conflict)
                case Ok():
                    return Ok(None)

        return await self._transactions.run(work)
