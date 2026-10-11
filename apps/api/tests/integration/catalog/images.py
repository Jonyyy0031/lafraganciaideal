"""Synthetic images for the photo tests, built with Pillow (no files, no real photos)."""

from io import BytesIO

from PIL import Image

RED = (220, 20, 20)
BLUE = (20, 20, 220)
EXIF_ORIENTATION = 0x0112


def encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def two_tone(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    """Left half red, right half blue."""
    image = Image.new(mode, size, BLUE)
    image.paste(RED, (0, 0, size[0] // 2, size[1]))
    return image


def jpeg(size: tuple[int, int] = (2000, 1500), *, orientation: int | None = None) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Synthetic Camera"  # Make
    if orientation is not None:
        exif[EXIF_ORIENTATION] = orientation
    return encode(two_tone(size), "JPEG", exif=exif.tobytes(), icc_profile=b"\x00" * 32)


def png(size: tuple[int, int] = (800, 800), mode: str = "RGB") -> bytes:
    return encode(two_tone(size, mode), "PNG")


def png_with_transparency(size: tuple[int, int] = (800, 800)) -> bytes:
    """Left half fully transparent, right half opaque blue."""
    image = Image.new("RGBA", size, (*BLUE, 255))
    image.paste((0, 0, 0, 0), (0, 0, size[0] // 2, size[1]))
    return encode(image, "PNG")


def webp(size: tuple[int, int] = (800, 800)) -> bytes:
    return encode(two_tone(size), "WEBP", quality=90)


def gif(size: tuple[int, int] = (800, 800)) -> bytes:
    return encode(two_tone(size).convert("P"), "GIF")


def bmp(size: tuple[int, int] = (800, 800)) -> bytes:
    return encode(two_tone(size), "BMP")


def open_webp(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image
