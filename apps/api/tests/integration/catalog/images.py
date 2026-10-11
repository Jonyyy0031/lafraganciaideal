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


def _icc_xyz(x: float, y: float, z: float) -> bytes:
    def s15(value: float) -> bytes:
        return round(value * 65536).to_bytes(4, "big", signed=True)

    return b"XYZ \x00\x00\x00\x00" + s15(x) + s15(y) + s15(z)


def display_p3_profile() -> bytes:
    """A minimal matrix/TRC ICC v2 profile with the Display P3 primaries (D50-adapted) and a 2.2
    gamma, built by hand: Pillow can only create the sRGB profile itself."""
    gamma = (
        b"curv\x00\x00\x00\x00" + (1).to_bytes(4, "big") + (0x0233).to_bytes(2, "big") + b"\x00\x00"
    )
    tags = {
        b"wtpt": _icc_xyz(0.9642, 1.0, 0.8249),
        b"rXYZ": _icc_xyz(0.5151, 0.2412, -0.0011),
        b"gXYZ": _icc_xyz(0.2920, 0.6922, 0.0419),
        b"bXYZ": _icc_xyz(0.1571, 0.0666, 0.7841),
        b"rTRC": gamma,
        b"gTRC": gamma,
        b"bTRC": gamma,
    }
    table, body = b"", b""
    offset = 128 + 4 + 12 * len(tags)
    for signature, data in tags.items():
        padded = data + b"\x00" * (-len(data) % 4)
        table += signature + offset.to_bytes(4, "big") + len(data).to_bytes(4, "big")
        body += padded
        offset += len(padded)
    header = bytearray(128)
    header[0:4] = offset.to_bytes(4, "big")
    header[8:12] = b"\x02\x20\x00\x00"  # version 2.2
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    header[36:40] = b"acsp"
    header[68:80] = (
        round(0.9642 * 65536).to_bytes(4, "big")
        + (65536).to_bytes(4, "big")
        + round(0.8249 * 65536).to_bytes(4, "big")
    )
    return bytes(header) + len(tags).to_bytes(4, "big") + table + body


def mpo(size: tuple[int, int] = (900, 700)) -> bytes:
    """A two-frame multi-picture JPEG (Pillow opens it as format `MPO`)."""
    buffer = BytesIO()
    two_tone(size).save(buffer, format="MPO", save_all=True, append_images=[two_tone(size)])
    return buffer.getvalue()
