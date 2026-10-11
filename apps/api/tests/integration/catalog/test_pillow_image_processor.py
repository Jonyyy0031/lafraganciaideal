"""Plan 006: `PillowImageProcessor` on real encoded images (decoded and encoded by Pillow)."""

from io import BytesIO

import pytest
from PIL import Image

from fragancia_api.modules.catalog.application.ports import ProcessedImage
from fragancia_api.modules.catalog.infrastructure.pillow_image_processor import (
    PillowImageProcessor,
)
from fragancia_api.shared.kernel import Err, Ok
from tests.integration.catalog import images

pytestmark = pytest.mark.integration

FORMATS = ["jpeg", "png", "webp"]


async def _ok(data: bytes) -> ProcessedImage:
    result = await PillowImageProcessor(max_bytes=5_000_000).process(data)
    assert isinstance(result, Ok), result
    return result.value


async def _err_code(data: bytes) -> tuple[str, object]:
    result = await PillowImageProcessor(max_bytes=5_000_000).process(data)
    assert isinstance(result, Err), result
    return str(result.error.code), result.error.details


def _color_close(actual: tuple[int, ...], expected: tuple[int, ...], tolerance: int = 40) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=False))


# --- renditions -------------------------------------------------------------------------------


async def test_a_large_jpeg_gets_a_1200_detail_and_a_400_card_in_the_same_aspect_ratio() -> None:
    image = await _ok(images.jpeg((2000, 1500)))

    detail, card = images.open_webp(image.detail), images.open_webp(image.card)
    assert detail.format == card.format == "WEBP"
    assert detail.size == (1200, 900)
    assert card.size == (400, 300)
    assert (image.width, image.height) == (1200, 900)


async def test_a_portrait_image_is_bounded_by_its_height() -> None:
    image = await _ok(images.jpeg((1500, 2000)))

    assert images.open_webp(image.detail).size == (900, 1200)
    assert images.open_webp(image.card).size == (300, 400)
    assert (image.width, image.height) == (900, 1200)


async def test_renditions_are_never_upscaled() -> None:
    image = await _ok(images.jpeg((700, 650)))

    assert images.open_webp(image.detail).size == (700, 650)
    assert images.open_webp(image.card).size == (400, 371)
    assert (image.width, image.height) == (700, 650)


async def test_an_image_of_exactly_the_minimum_side_is_accepted_and_kept_at_its_size() -> None:
    image = await _ok(images.png((600, 600)))

    assert images.open_webp(image.detail).size == (600, 600)
    assert images.open_webp(image.card).size == (400, 400)


@pytest.mark.parametrize("size", [(600, 1000), (1000, 600)])
async def test_the_minimum_applies_to_the_shorter_side_only(size: tuple[int, int]) -> None:
    assert isinstance(
        await PillowImageProcessor(max_bytes=5_000_000).process(images.jpeg(size)), Ok
    )


@pytest.mark.parametrize("size", [(599, 2000), (2000, 599), (500, 800), (1, 1)])
async def test_an_image_with_a_shorter_side_under_600_is_too_small(size: tuple[int, int]) -> None:
    code, details = await _err_code(images.jpeg(size))

    assert code == "CATALOG_PHOTO_TOO_SMALL"
    assert details == {"min_side": 600}


@pytest.mark.parametrize(
    ("make", "source_format"),
    [
        (lambda: images.jpeg((900, 900)), "JPEG"),
        (lambda: images.png((900, 900)), "PNG"),
        (lambda: images.webp((900, 900)), "WEBP"),
    ],
)
async def test_jpeg_png_and_webp_inputs_all_come_out_as_webp(make, source_format: str) -> None:  # type: ignore[no-untyped-def]
    data = make()
    assert Image.open(BytesIO(data)).format == source_format

    image = await _ok(data)

    assert images.open_webp(image.card).format == "WEBP"
    assert images.open_webp(image.detail).format == "WEBP"


async def test_the_red_and_blue_halves_survive_the_conversion() -> None:
    image = await _ok(images.jpeg((1200, 900)))

    detail = images.open_webp(image.detail).convert("RGB")
    assert _color_close(detail.getpixel((100, 450)), images.RED)  # type: ignore[arg-type]
    assert _color_close(detail.getpixel((1100, 450)), images.BLUE)  # type: ignore[arg-type]


# --- metadata ---------------------------------------------------------------------------------


async def test_the_source_has_exif_and_an_icc_profile_but_the_renditions_have_none() -> None:
    data = images.jpeg((1400, 1000))
    source = Image.open(BytesIO(data))
    assert source.getexif().get(0x010F) == "Synthetic Camera"
    assert "icc_profile" in source.info

    image = await _ok(data)

    for rendition in (image.card, image.detail):
        decoded = images.open_webp(rendition)
        assert dict(decoded.getexif()) == {}
        assert "exif" not in decoded.info
        assert "icc_profile" not in decoded.info


# --- orientation ------------------------------------------------------------------------------


async def test_exif_orientation_is_applied_before_sizing() -> None:
    # 1400x1000 stored, orientation 6 = displayed rotated 90 degrees clockwise: 1000x1400.
    image = await _ok(images.jpeg((1400, 1000), orientation=6))

    assert images.open_webp(image.detail).size == (857, 1200)
    assert images.open_webp(image.card).size == (286, 400)
    assert (image.width, image.height) == (857, 1200)


async def test_exif_orientation_rotates_the_pixels_not_just_the_size() -> None:
    # The left half is red; rotated 90 degrees clockwise it becomes the top half.
    image = await _ok(images.jpeg((1200, 800), orientation=6))

    detail = images.open_webp(image.detail).convert("RGB")
    assert detail.size == (800, 1200)
    assert _color_close(detail.getpixel((400, 100)), images.RED)  # type: ignore[arg-type]
    assert _color_close(detail.getpixel((400, 1100)), images.BLUE)  # type: ignore[arg-type]


async def test_an_image_without_orientation_keeps_its_pixels() -> None:
    image = await _ok(images.jpeg((1200, 800)))

    detail = images.open_webp(image.detail).convert("RGB")
    assert detail.size == (1200, 800)
    assert _color_close(detail.getpixel((100, 400)), images.RED)  # type: ignore[arg-type]
    assert _color_close(detail.getpixel((1100, 400)), images.BLUE)  # type: ignore[arg-type]


# --- alpha and color modes --------------------------------------------------------------------


async def test_a_png_with_transparency_stays_transparent() -> None:
    image = await _ok(images.png_with_transparency((1000, 800)))

    for rendition, probe in ((image.detail, (100, 400)), (image.card, (50, 200))):
        decoded = images.open_webp(rendition)
        assert decoded.mode == "RGBA"
        assert decoded.getpixel(probe)[3] == 0  # type: ignore[index]
        assert decoded.getpixel((decoded.width - 5, probe[1]))[3] == 255  # type: ignore[index]


async def test_an_opaque_png_comes_out_without_an_alpha_channel() -> None:
    image = await _ok(images.png((900, 900)))

    assert images.open_webp(image.detail).mode == "RGB"
    assert images.open_webp(image.card).mode == "RGB"


async def test_a_palette_png_with_a_transparent_color_keeps_its_transparency() -> None:
    source = Image.new("P", (800, 800), 0)
    source.putpalette([255, 0, 0, 0, 0, 255] + [0] * 250 * 3)
    source.paste(1, (400, 0, 800, 800))
    data = images.encode(source, "PNG", transparency=0)

    image = await _ok(data)

    decoded = images.open_webp(image.detail)
    assert decoded.mode == "RGBA"
    assert decoded.getpixel((100, 400))[3] == 0  # type: ignore[index]
    assert decoded.getpixel((700, 400))[3] == 255  # type: ignore[index]


@pytest.mark.parametrize("mode", ["L", "CMYK"])
async def test_grayscale_and_cmyk_jpegs_come_out_as_rgb(mode: str) -> None:
    data = images.encode(images.two_tone((900, 900)).convert(mode), "JPEG")

    image = await _ok(data)

    assert images.open_webp(image.detail).mode == "RGB"


# --- what is rejected -------------------------------------------------------------------------


async def test_a_gif_is_a_format_error_even_when_it_is_large_enough() -> None:
    code, details = await _err_code(images.gif((900, 900)))

    assert code == "CATALOG_PHOTO_FORMAT_INVALID"
    assert details == {"formats": FORMATS}


async def test_a_bmp_is_a_format_error() -> None:
    assert (await _err_code(images.bmp((900, 900))))[0] == "CATALOG_PHOTO_FORMAT_INVALID"


async def test_a_small_gif_is_a_format_error_not_a_size_error() -> None:
    assert (await _err_code(images.gif((100, 100))))[0] == "CATALOG_PHOTO_FORMAT_INVALID"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not an image at all, just text renamed .jpg", id="text"),
        pytest.param(b"\xff\xd8\xff", id="jpeg-signature-only"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, id="png-signature-garbage"),
        pytest.param(bytes(range(256)) * 40, id="binary-noise"),
    ],
)
async def test_garbage_is_a_format_error(data: bytes) -> None:
    code, details = await _err_code(data)

    assert code == "CATALOG_PHOTO_FORMAT_INVALID"
    assert details == {"formats": FORMATS}


@pytest.mark.parametrize("keep", [0.2, 0.6])
async def test_a_truncated_jpeg_is_a_format_error(keep: float) -> None:
    data = images.jpeg((1400, 1000))

    code, _ = await _err_code(data[: int(len(data) * keep)])

    assert code == "CATALOG_PHOTO_FORMAT_INVALID"


async def test_a_decompression_bomb_is_a_format_error() -> None:
    """A 1-bit PNG of 196 million pixels is a few hundred KB, but Pillow's guard refuses it."""
    data = images.encode(Image.new("1", (14_000, 14_000)), "PNG", optimize=False)
    assert len(data) < 10_485_760

    code, _ = await _err_code(data)

    assert code == "CATALOG_PHOTO_FORMAT_INVALID"


async def test_the_decompression_guard_is_not_disabled_for_the_whole_process() -> None:
    assert Image.MAX_IMAGE_PIXELS is not None
