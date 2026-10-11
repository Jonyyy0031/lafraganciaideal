from collections.abc import Sequence
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
    PublicBrand,
    PublicConcentration,
    PublicOlfactoryFamily,
    PublicPerfume,
    PublicPerfumePage,
)
from fragancia_api.modules.catalog.domain.errors import PhotoFormatInvalid, PhotoTooSmall
from fragancia_api.shared.kernel import Result


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
    ) -> Result[ProcessedImage, PhotoFormatInvalid | PhotoTooSmall]: ...


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
