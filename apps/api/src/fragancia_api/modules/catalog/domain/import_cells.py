"""Cell rules of the catalog import file (pure: the standard library and `naming.slugify`).

The template has one row per presentation and these Spanish headers (`COLUMNS`). Headers are
matched by `header_key` (their slug), so case, accents and punctuation do not matter; unknown
headers are ignored.

Each parsing function returns a `Result` and never raises. Words ("Mujer", "Sobre pedido",
"Sí") are matched by slug too. Sale dates are whole days in Cancún time (decision 66): a sale
starts at 00:00 of "Oferta desde" and ends at 00:00 of the day after "Oferta hasta". Prices are
pesos with at most two decimals (decision 67).
"""

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fragancia_api.modules.catalog.domain.errors import ImportImageUrlInvalid, ImportValueInvalid
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.shared.kernel import Err, Ok, Result

COLUMNS: tuple[str, ...] = (
    "Marca",
    "Nombre",
    "Concentración",
    "Género",
    "Familia olfativa",
    "ML",
    "Precio",
    "Precio oferta",
    "Oferta desde",
    "Oferta hasta",
    "Disponibilidad",
    "Entrega mín. (días)",
    "Entrega máx. (días)",
    "Existencias",
    "Publicado",
    "Notas de salida",
    "Notas de corazón",
    "Notas de fondo",
    "Descripción",
    "Imagen 1",
    "Imagen 2",
    "Imagen 3",
)

REQUIRED_COLUMNS: tuple[str, ...] = (
    "Marca",
    "Nombre",
    "Concentración",
    "Género",
    "Familia olfativa",
    "ML",
    "Precio",
    "Disponibilidad",
)

IMAGE_COLUMNS: tuple[str, ...] = ("Imagen 1", "Imagen 2", "Imagen 3")

# Mirrors inventory's `QUANTITY_MAX` (a test pins the two).
STOCK_COUNT_MAX = 100_000
TEXT_MAX_LENGTH = 4000
IMAGE_URL_MAX_LENGTH = 2000
SALE_TIMEZONE = ZoneInfo("America/Cancun")

type Cell = str | int | float | bool | date | datetime | None

_PESOS = re.compile(r"^\d+(\.\d{1,2})?$")
_GENDERS = {"mujer": "women", "hombre": "men", "unisex": "unisex"}
_AVAILABILITIES = {"en-existencia": "in_stock", "sobre-pedido": "made_to_order"}
_PUBLISHED = {"si": True, "no": False}


def header_key(text: str) -> str:
    """The key a header is matched by: its slug."""
    return slugify(text)


def _integral(value: float) -> bool:
    return value.is_integer()  # False for NaN and the infinities


def text(cell: Cell) -> Result[str, ImportValueInvalid]:
    """A cell as text: empty for None, stripped for a string, the digits of a whole number."""
    match cell:
        case None:
            value = ""
        case bool():
            return Err(ImportValueInvalid())
        case str():
            value = cell.strip()
        case int():
            value = str(cell)
        case float() if _integral(cell):
            value = str(int(cell))
        case _:
            return Err(ImportValueInvalid())
    if len(value) > TEXT_MAX_LENGTH:
        return Err(ImportValueInvalid(details={"max_length": TEXT_MAX_LENGTH}))
    return Ok(value)


def whole_number(cell: Cell) -> Result[int, ImportValueInvalid]:
    """An int, an integral float or a digit-only string."""
    match cell:
        case bool():
            return Err(ImportValueInvalid())
        case int():
            return Ok(cell)
        case float() if _integral(cell):
            return Ok(int(cell))
        case str() if cell.strip().isdigit() and cell.strip().isascii():
            return Ok(int(cell.strip()))
        case _:
            return Err(ImportValueInvalid())


def stock_count(cell: Cell) -> Result[int, ImportValueInvalid]:
    """A physical count: a whole number from 0 to `STOCK_COUNT_MAX`."""
    match whole_number(cell):
        case Ok(value) if 0 <= value <= STOCK_COUNT_MAX:
            return Ok(value)
        case Ok():
            return Err(ImportValueInvalid(details={"min": 0, "max": STOCK_COUNT_MAX}))
        case error:
            return error


def pesos_to_cents(cell: Cell) -> Result[int, ImportValueInvalid]:
    """Pesos with at most two decimals, as cents; negative or non-finite values are invalid."""
    match cell:
        case bool():
            return Err(ImportValueInvalid())
        case int():
            amount = Decimal(cell)
        case float():
            try:
                amount = Decimal(str(cell))
            except InvalidOperation:
                return Err(ImportValueInvalid())
        case str() if _PESOS.match(cell.strip()):
            amount = Decimal(cell.strip())
        case _:
            return Err(ImportValueInvalid())
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        return Err(ImportValueInvalid())
    return Ok(int(amount * 100))


def sale_day(cell: Cell) -> Result[date, ImportValueInvalid]:
    """A date, a datetime at exactly 00:00:00, or a `DD/MM/AAAA` or `AAAA-MM-DD` string."""
    match cell:
        case datetime():
            if cell.time() != time(0, 0) or cell.tzinfo is not None:
                return Err(ImportValueInvalid())
            return Ok(cell.date())
        case date():
            return Ok(cell)
        case str():
            raw = cell.strip()
            for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
                try:
                    return Ok(datetime.strptime(raw, pattern).date())
                except ValueError:
                    continue
            return Err(ImportValueInvalid())
        case _:
            return Err(ImportValueInvalid())


def sale_window(starts: date | None, ends: date | None) -> tuple[datetime | None, datetime | None]:
    """The sale window in Cancún time: 00:00 of `starts`, and 00:00 of the day after `ends`."""
    starts_at = (
        datetime.combine(starts, time(0, 0), tzinfo=SALE_TIMEZONE) if starts is not None else None
    )
    ends_at = (
        datetime.combine(ends + timedelta(days=1), time(0, 0), tzinfo=SALE_TIMEZONE)
        if ends is not None
        else None
    )
    return starts_at, ends_at


def gender(value: str) -> Result[str, ImportValueInvalid]:
    """`Mujer`, `Hombre` or `Unisex` as `women`, `men` or `unisex`."""
    match _GENDERS.get(slugify(value)):
        case None:
            return Err(ImportValueInvalid())
        case found:
            return Ok(found)


def availability(value: str) -> Result[str, ImportValueInvalid]:
    """`En existencia` or `Sobre pedido` as `in_stock` or `made_to_order`."""
    match _AVAILABILITIES.get(slugify(value)):
        case None:
            return Err(ImportValueInvalid())
        case found:
            return Ok(found)


def published(value: str) -> Result[bool | None, ImportValueInvalid]:
    """`Sí` or `No`; an empty cell is None (leave it as it is)."""
    if value.strip() == "":
        return Ok(None)
    match _PUBLISHED.get(slugify(value)):
        case None:
            return Err(ImportValueInvalid())
        case found:
            return Ok(found)


def notes(value: str) -> list[str]:
    """Comma-separated notes, each stripped; empty items are dropped."""
    return [item.strip() for item in value.split(",") if item.strip()]


def image_url(value: str) -> Result[str | None, ImportImageUrlInvalid]:
    """None for an empty cell; otherwise an http(s) URL of at most 2000 characters."""
    url = value.strip()
    if url == "":
        return Ok(None)
    if not url.startswith(("http://", "https://")) or len(url) > IMAGE_URL_MAX_LENGTH:
        return Err(ImportImageUrlInvalid(details={"max_length": IMAGE_URL_MAX_LENGTH}))
    return Ok(url)
