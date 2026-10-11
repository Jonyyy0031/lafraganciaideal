from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from fragancia_api.modules.catalog.contracts import (
    AdminBrandPage,
    AdminConcentrationPage,
    AdminOlfactoryFamilyPage,
    AdminPerfume,
    AdminPerfumePage,
    CatalogImportResponse,
    PublicBrand,
    PublicConcentration,
    PublicOlfactoryFamily,
    PublicPerfume,
    PublicPerfumePage,
)
from fragancia_api.modules.catalog.domain.catalog_import import CatalogImport, ImportIssue
from fragancia_api.modules.catalog.domain.errors import (
    PhotoFormatInvalid,
    PhotoTooLarge,
    PhotoTooSmall,
)
from fragancia_api.modules.catalog.domain.import_cells import Cell
from fragancia_api.shared.kernel import DomainError, Result


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """The two WebP renditions of a photo; width and height are those of the detail one."""

    card: bytes
    detail: bytes
    width: int
    height: int


class ImageProcessor(Protocol):
    """Validates an uploaded image and renders its card and detail renditions."""

    async def process(
        self, data: bytes
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooLarge | PhotoTooSmall]: ...


class MediaStorage(Protocol):
    """Object storage for media files."""

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def delete(self, keys: Sequence[str]) -> None:
        """Delete the objects; a missing key is not an error."""
        ...


type PublicPerfumeSort = Literal["name", "price_asc", "price_desc", "newest"]


@dataclass(frozen=True, slots=True)
class PublicPerfumeFilters:
    """What the public perfume list is asked for; an empty tuple or None means no filter."""

    q: str | None
    brands: tuple[str, ...]
    families: tuple[str, ...]
    genders: tuple[str, ...]
    min_price_cents: int | None
    max_price_cents: int | None
    sort: PublicPerfumeSort
    page: int
    size: int


class BrandQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def list_active(self) -> list[PublicBrand]:
        """Active brands ordered by name (case-insensitive)."""
        ...

    async def list_all(self, *, page: int, size: int) -> AdminBrandPage:
        """Every brand ordered by name (case-insensitive), one page at a time."""
        ...


class OlfactoryFamilyQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def list_active(self) -> list[PublicOlfactoryFamily]:
        """Active families ordered by name (case-insensitive)."""
        ...

    async def list_all(self, *, page: int, size: int) -> AdminOlfactoryFamilyPage:
        """Every family ordered by name (case-insensitive), one page at a time."""
        ...


class ConcentrationQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def list_active(self) -> list[PublicConcentration]:
        """Active concentrations ordered by name (case-insensitive)."""
        ...

    async def list_all(self, *, page: int, size: int) -> AdminConcentrationPage:
        """Every concentration ordered by name (case-insensitive), one page at a time."""
        ...


class PerfumeQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def list_admin(self, *, page: int, size: int, archived: bool) -> AdminPerfumePage:
        """The non-archived perfumes (or the archived ones when `archived`), ordered by brand
        name, then perfume name (case-insensitive), then id; one page at a time."""
        ...

    async def get_admin(self, perfume_id: UUID) -> AdminPerfume | None:
        """The perfume with its brand, concentration, family and presentations (by ml)."""
        ...

    async def list_public(
        self, filters: PublicPerfumeFilters, *, now: datetime
    ) -> PublicPerfumePage:
        """Visible perfumes (published, not archived, brand active; the family and the
        concentration do not matter) with at least one active presentation, one page at a time.
        Ordering: `name` is brand name, then perfume name (both case-insensitive), then id;
        `price_asc`/`price_desc` is `price_from` then name; `newest` is `first_published_at`
        descending, then id."""
        ...

    async def get_public(self, slug: str, *, now: datetime) -> PublicPerfume | None:
        """A visible perfume with its active presentations (by ml), matched by its current slug
        first and then by its slug history; the answer always carries the current slug."""
        ...


# Catalog import (plan 007).


@dataclass(frozen=True, slots=True)
class SheetRows:
    """The data rows of an import sheet: (Excel row number, cells by `header_key`), the header
    keys found and the keys that appear more than once."""

    rows: list[tuple[int, dict[str, Cell]]]
    headers: list[str]
    duplicated: list[str]


class SpreadsheetReader(Protocol):
    async def read(self, data: bytes, *, max_rows: int) -> Result[SheetRows, ImportIssue]:
        """The rows of the first sheet, or a file-level issue: unreadable, empty or more than
        `max_rows` data rows."""
        ...


class TemplateWriter(Protocol):
    async def build(self, families: list[str], concentrations: list[str]) -> bytes:
        """The .xlsx template, with dropdowns of these family and concentration names."""
        ...


class ImageDownloader(Protocol):
    async def fetch(self, url: str) -> Result[bytes, DomainError]:
        """The body of a public image URL, or `CATALOG_IMPORT_IMAGE_URL_INVALID`,
        `CATALOG_IMPORT_IMAGE_HOST_BLOCKED`, `CATALOG_IMPORT_IMAGE_UNREACHABLE` or
        `CATALOG_PHOTO_TOO_LARGE`."""
        ...


@dataclass(frozen=True, slots=True)
class ReferenceItem:
    """A brand, family or concentration as the import matches it; `alt_slug` is the
    abbreviation slug of a concentration."""

    id: UUID
    slug: str
    alt_slug: str | None
    name: str
    is_active: bool


type PerfumeIdentity = tuple[UUID, str, UUID]  # (brand_id, name_slug, concentration_id)


@dataclass(frozen=True, slots=True)
class ExistingPerfume:
    """A stored perfume: `presentations` maps ml → (id, is_active) and `photos` maps
    position → (id, source_url)."""

    id: UUID
    is_archived: bool
    family_id: UUID
    presentations: dict[int, tuple[UUID, bool]]
    photos: dict[int, tuple[UUID, str | None]]


class ImportReferences(Protocol):
    """Read-only lookups for validating an import."""

    async def brands(self) -> list[ReferenceItem]: ...

    async def families(self) -> list[ReferenceItem]: ...

    async def concentrations(self) -> list[ReferenceItem]: ...

    async def existing_perfumes(
        self, identities: Collection[PerfumeIdentity]
    ) -> dict[PerfumeIdentity, ExistingPerfume]:
        """The stored perfumes with these identities (archived ones included)."""
        ...


class CatalogImportRepository(Protocol):
    """Write side. Joins the active transaction."""

    async def add(self, catalog_import: CatalogImport) -> None: ...

    async def get_for_update(self, import_id: UUID) -> CatalogImport | None:
        """The import, with its row locked until the transaction ends."""
        ...

    async def claim_next_pending(self) -> CatalogImport | None:
        """The oldest pending import by `created_at`, locked with `FOR UPDATE SKIP LOCKED`."""
        ...

    async def list_stale_validating(self, before: datetime) -> list[CatalogImport]:
        """Imports validating since before `before`, locked with `SKIP LOCKED`."""
        ...

    async def list_expired_ready(self, now: datetime) -> list[CatalogImport]:
        """Ready imports with `expires_at <= now`, locked with `SKIP LOCKED`."""
        ...

    async def save(self, catalog_import: CatalogImport) -> None: ...


class CatalogImportQueries(Protocol):
    async def get(self, import_id: UUID) -> CatalogImportResponse | None: ...
