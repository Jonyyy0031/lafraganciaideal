from typing import Protocol
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
)


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
