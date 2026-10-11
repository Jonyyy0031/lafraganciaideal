"""In-memory media adapters for unit tests."""

from collections.abc import Sequence

from fragancia_api.modules.catalog.application.ports import ProcessedImage
from fragancia_api.modules.catalog.domain.errors import (
    PhotoFormatInvalid,
    PhotoTooLarge,
    PhotoTooSmall,
)
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryMediaStorage:
    """Records puts and deletes; `fail_puts` / `fail_deletes` make the matching call raise."""

    def __init__(self, *, fail_puts: bool = False, fail_deletes: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.deleted: list[str] = []
        self.fail_puts = fail_puts
        self.fail_deletes = fail_deletes

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail_puts:
            raise RuntimeError("storage put failed")
        self.objects[key] = data
        self.content_types[key] = content_type

    async def delete(self, keys: Sequence[str]) -> None:
        if self.fail_deletes:
            raise RuntimeError("storage delete failed")
        for key in keys:
            self.objects.pop(key, None)
            self.content_types.pop(key, None)
            self.deleted.append(key)


class FakeImageProcessor:
    """Returns a fixed `ProcessedImage`, or the configured Err."""

    def __init__(
        self,
        image: ProcessedImage | None = None,
        *,
        error: PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall | None = None,
    ) -> None:
        self.image = image or ProcessedImage(card=b"card", detail=b"detail", width=1200, height=900)
        self.error = error
        self.calls: list[bytes] = []

    async def process(
        self, data: bytes
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall]:
        self.calls.append(data)
        if self.error is not None:
            return Err(self.error)
        return Ok(self.image)
