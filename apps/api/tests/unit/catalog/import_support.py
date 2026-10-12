"""Builders shared by the catalog import tests (validator, commands, http)."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from fragancia_api.modules.catalog.application.import_validation import ImportValidator
from fragancia_api.modules.catalog.application.ports import (
    ExistingPerfume,
    PerfumeIdentity,
    ProcessedImage,
    ReferenceItem,
    SheetRows,
)
from fragancia_api.modules.catalog.domain import import_cells as cells
from fragancia_api.modules.catalog.domain.catalog_import import (
    ImportPlan,
    PlannedPerfume,
    PlannedPhoto,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.errors import (
    PhotoFormatInvalid,
    PhotoTooLarge,
    PhotoTooSmall,
)
from fragancia_api.modules.catalog.domain.perfume import Gender, photo_keys
from fragancia_api.modules.catalog.infrastructure.in_memory_imports import (
    FakeImageDownloader,
    InMemoryImportReferences,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_media import (
    FakeImageProcessor,
    InMemoryMediaStorage,
)
from fragancia_api.shared.kernel import Err, Ok, Result

BRAND = ReferenceItem(UUID(int=11), "versace", None, "Versace", True)
BRAND_B = ReferenceItem(UUID(int=12), "dior", None, "Dior", True)
BRAND_ARCHIVED = ReferenceItem(UUID(int=13), "chanel", None, "Chanel", False)
FAMILY = ReferenceItem(UUID(int=21), "aromatica", None, "Aromática", True)
FAMILY_B = ReferenceItem(UUID(int=22), "floral", None, "Floral", True)
FAMILY_ARCHIVED = ReferenceItem(UUID(int=23), "citrica", None, "Cítrica", False)
CONCENTRATION = ReferenceItem(UUID(int=31), "eau-de-toilette", "edt", "Eau de Toilette", True)
CONCENTRATION_B = ReferenceItem(UUID(int=32), "eau-de-parfum", "edp", "Eau de Parfum", True)
CONCENTRATION_ARCHIVED = ReferenceItem(UUID(int=33), "colonia", "col", "Colonia", False)

IMAGE = ProcessedImage(card=b"card-bytes", detail=b"detail-bytes", width=1200, height=900)
STORED_PERFUME_ID = UUID(int=900)
URL_1 = "https://img.test/one.jpg"
URL_2 = "https://img.test/two.jpg"
URL_3 = "https://img.test/three.jpg"

# A valid row of the perfume Versace Eros EDT 100 ml; tests override single columns.
VALID: dict[str, cells.Cell] = {
    "Marca": "Versace",
    "Nombre": "Eros",
    "Concentración": "Eau de Toilette",
    "Género": "Hombre",
    "Familia olfativa": "Aromática",
    "ML": 100,
    "Precio": 2500,
    "Disponibilidad": "En existencia",
}


def row(**overrides: cells.Cell) -> dict[str, cells.Cell]:
    """A valid row by header text, with columns replaced; underscores stand for the spaces and
    accents of the keyword (`Familia_olfativa`), and `None` blanks a column out."""
    values: dict[str, cells.Cell] = dict(VALID)
    for key, value in overrides.items():
        values[_column(key)] = value
    return values


def _column(key: str) -> str:
    by_key = {cells.header_key(column).replace("-", "_"): column for column in cells.COLUMNS}
    return by_key[cells.header_key(key.replace("_", " ")).replace("-", "_")]


def sheet(*rows: dict[str, cells.Cell], drop: tuple[str, ...] = ()) -> SheetRows:
    """The rows numbered from Excel row 2, every column of the template present (header row 1),
    except the `drop`ped ones."""
    keys = [cells.header_key(c) for c in cells.COLUMNS if c not in drop]
    numbered = [
        (
            number,
            {
                cells.header_key(column): value
                for column, value in values.items()
                if column not in drop
            },
        )
        for number, values in enumerate(rows, start=2)
    ]
    return SheetRows(rows=numbered, headers=keys, duplicated=[])


class PerPayloadImageProcessor(FakeImageProcessor):
    """Like `FakeImageProcessor`, but an error can be set per input bytes."""

    def __init__(self) -> None:
        super().__init__(IMAGE)
        self.errors: dict[bytes, PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall] = {}

    async def process(
        self, data: bytes
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall]:
        self.calls.append(data)
        if data in self.errors:
            return Err(self.errors[data])
        return Ok(self.image)


def existing(
    perfume_id: UUID = STORED_PERFUME_ID,
    *,
    archived: bool = False,
    family: ReferenceItem = FAMILY,
    presentations: dict[int, tuple[UUID, bool]] | None = None,
    photos: dict[int, tuple[UUID, str | None]] | None = None,
) -> ExistingPerfume:
    return ExistingPerfume(
        id=perfume_id,
        is_archived=archived,
        family_id=family.id,
        presentations=presentations or {},
        photos=photos or {},
    )


def identity(
    brand: ReferenceItem = BRAND,
    name_slug: str = "eros",
    concentration: ReferenceItem = CONCENTRATION,
) -> PerfumeIdentity:
    return (brand.id, name_slug, concentration.id)


@dataclass
class ValidatorRig:
    references: InMemoryImportReferences
    downloader: FakeImageDownloader
    images: PerPayloadImageProcessor
    storage: InMemoryMediaStorage
    validator: ImportValidator = field(init=False)

    def __post_init__(self) -> None:
        self.validator = ImportValidator(
            references=self.references,
            downloader=self.downloader,
            images=self.images,
            storage=self.storage,
            image_budget=timedelta(minutes=20),
        )


def make_rig(
    *,
    perfumes: dict[PerfumeIdentity, ExistingPerfume] | None = None,
    downloads: dict[str, Any] | None = None,
    brands: list[ReferenceItem] | None = None,
    families: list[ReferenceItem] | None = None,
    concentrations: list[ReferenceItem] | None = None,
) -> ValidatorRig:
    return ValidatorRig(
        references=InMemoryImportReferences(
            brands=brands if brands is not None else [BRAND, BRAND_B, BRAND_ARCHIVED],
            families=families if families is not None else [FAMILY, FAMILY_B, FAMILY_ARCHIVED],
            concentrations=(
                concentrations
                if concentrations is not None
                else [CONCENTRATION, CONCENTRATION_B, CONCENTRATION_ARCHIVED]
            ),
            perfumes=perfumes,
        ),
        downloader=FakeImageDownloader(downloads or {}),
        images=PerPayloadImageProcessor(),
        storage=InMemoryMediaStorage(),
    )


# --- a plan with staged photos (3 photos, so 6 staged objects) ---------------------------------


def planned_photo(perfume_id: UUID, photo_id: UUID, position: int = 1) -> PlannedPhoto:
    card, detail = photo_keys(perfume_id, photo_id)
    return PlannedPhoto(position, photo_id, "https://x.test/a.jpg", card, detail, 1200, 900, None)


def planned_perfume(index: int, photos: int) -> PlannedPerfume:
    perfume_id = UUID(int=index)
    return PlannedPerfume(
        perfume_id=perfume_id,
        is_new=True,
        brand_id=UUID(int=100),
        concentration_id=UUID(int=101),
        family_id=UUID(int=102),
        name="Eros",
        gender=Gender.MEN,
        description="",
        top_notes=(),
        heart_notes=(),
        base_notes=(),
        publish=None,
        first_row=2,
        presentations=(
            PlannedPresentation(None, 100, 250_000, None, None, None, "in_stock", None, None, 2),
        ),
        photos=tuple(
            planned_photo(perfume_id, UUID(int=index * 10 + n), n + 1) for n in range(photos)
        ),
    )


PLAN = ImportPlan(perfumes=(planned_perfume(1, 2), planned_perfume(2, 0), planned_perfume(3, 1)))
