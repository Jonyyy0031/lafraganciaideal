import asyncio
from io import BytesIO
from typing import cast

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from fragancia_api.modules.catalog.application.ports import ProcessedImage
from fragancia_api.modules.catalog.domain.errors import (
    PhotoFormatInvalid,
    PhotoTooLarge,
    PhotoTooSmall,
)
from fragancia_api.modules.catalog.domain.perfume import CARD_SIDE, DETAIL_SIDE, PHOTO_MIN_SIDE
from fragancia_api.shared.kernel import Err, Ok, Result

_OPEN_FORMATS = ["JPEG", "PNG", "WEBP"]  # MPO has no opener of its own: the JPEG one yields it
_FORMATS = {*_OPEN_FORMATS, "MPO"}  # MPO (phones): Pillow loads frame 0, the main picture
PHOTO_MAX_PIXELS = 40_000_000
_HIGH_DEPTH_MODES = ("I;16", "I;16L", "I;16B", "I;16N", "I", "F")
_QUALITY = 82
_FORMAT_NAMES = ["jpeg", "png", "webp"]


class PillowImageProcessor:
    """Validates an upload with Pillow and renders the card and detail WebP renditions.

    Decoding and encoding are CPU-bound, so they run in a worker thread (`asyncio.to_thread`).
    Pillow's decompression-bomb guard stays on; a triggered guard is an invalid format.
    """

    def __init__(self, *, max_bytes: int) -> None:
        self._max_bytes = max_bytes

    async def process(
        self, data: bytes
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall]:
        return await asyncio.to_thread(self._process, data)

    def _process(
        self, data: bytes
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall]:
        try:
            with Image.open(BytesIO(data), formats=_OPEN_FORMATS) as opened:
                if opened.format not in _FORMATS:
                    return Err(_format_invalid())
                # Checked from the header, before any pixel is decoded.
                if opened.width * opened.height > PHOTO_MAX_PIXELS:
                    return Err(
                        PhotoTooLarge(
                            details={
                                "max_bytes": self._max_bytes,
                                "max_pixels": PHOTO_MAX_PIXELS,
                            }
                        )
                    )
                opened.load()
                image = ImageOps.exif_transpose(opened)
                if min(image.size) < PHOTO_MIN_SIDE:
                    return Err(PhotoTooSmall(details={"min_side": PHOTO_MIN_SIDE}))
                image = _to_8bit(image)
                has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
                image = _to_srgb(image.convert("RGBA" if has_alpha else "RGB"))
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


def _to_8bit(image: Image.Image) -> Image.Image:
    """Scale 16-bit, 32-bit and float grayscale to 8-bit `L` (scaling, not clipping)."""
    if image.mode not in _HIGH_DEPTH_MODES:
        return image
    if image.mode.startswith("I;16"):
        # Full 16-bit range maps to 0..255.
        return image.convert("I").point(lambda value: value * (1 / 257)).convert("L")
    low, high = cast(tuple[float, float], image.getextrema())
    if image.mode == "I" and 0 <= low and high <= 255:
        return image.convert("L")
    span = (high - low) or 1
    scale = 255 / span
    return image.point(lambda value: (value - low) * scale).convert("L")


def _to_srgb(image: Image.Image) -> Image.Image:
    """Convert an embedded ICC profile to sRGB, then drop it; on a bad profile keep the pixels."""
    icc = image.info.get("icc_profile")
    if not icc:
        return image
    mode = image.mode  # already RGB or RGBA
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        converted = ImageCms.profileToProfile(
            image,
            source,
            target,
            renderingIntent=ImageCms.Intent.PERCEPTUAL,
            outputMode=mode,
        )
    except ImageCms.PyCMSError, OSError, ValueError:
        return image
    if converted is None:
        return image
    converted.info.pop("icc_profile", None)
    return converted
