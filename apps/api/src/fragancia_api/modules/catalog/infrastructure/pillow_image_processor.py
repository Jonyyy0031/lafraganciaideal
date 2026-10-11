import asyncio
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from fragancia_api.modules.catalog.application.ports import ProcessedImage
from fragancia_api.modules.catalog.domain.errors import PhotoFormatInvalid, PhotoTooSmall
from fragancia_api.modules.catalog.domain.perfume import CARD_SIDE, DETAIL_SIDE, PHOTO_MIN_SIDE
from fragancia_api.shared.kernel import Err, Ok, Result

_FORMATS = {"JPEG", "PNG", "WEBP"}
_QUALITY = 82
_FORMAT_NAMES = ["jpeg", "png", "webp"]


class PillowImageProcessor:
    """Validates an upload with Pillow and renders the card and detail WebP renditions.

    Decoding and encoding are CPU-bound, so they run in a worker thread (`asyncio.to_thread`).
    Pillow's decompression-bomb guard stays on; a triggered guard is an invalid format.
    """

    async def process(
        self, data: bytes
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooSmall]:
        return await asyncio.to_thread(self._process, data)

    def _process(self, data: bytes) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooSmall]:
        try:
            with Image.open(BytesIO(data)) as opened:
                if opened.format not in _FORMATS:
                    return Err(_format_invalid())
                opened.load()
                image = ImageOps.exif_transpose(opened)
                if min(image.size) < PHOTO_MIN_SIDE:
                    return Err(PhotoTooSmall(details={"min_side": PHOTO_MIN_SIDE}))
                has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
                image = image.convert("RGBA" if has_alpha else "RGB")
                card = _render(image, CARD_SIDE)
                detail_image = image.copy()
                detail_image.thumbnail((DETAIL_SIDE, DETAIL_SIDE), Image.Resampling.LANCZOS)
                width, height = detail_image.size
                detail = _encode(detail_image)
        except UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError:
            return Err(_format_invalid())
        return Ok(ProcessedImage(card=card, detail=detail, width=width, height=height))


def _format_invalid() -> PhotoFormatInvalid:
    return PhotoFormatInvalid(details={"formats": _FORMAT_NAMES})


def _render(image: Image.Image, side: int) -> bytes:
    scaled = image.copy()
    scaled.thumbnail((side, side), Image.Resampling.LANCZOS)
    return _encode(scaled)


def _encode(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=_QUALITY, method=6)
    return buffer.getvalue()
