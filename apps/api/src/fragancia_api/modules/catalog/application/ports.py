from typing import Protocol

from fragancia_api.modules.catalog.contracts import AdminBrandPage, PublicBrand


class BrandQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def list_active(self) -> list[PublicBrand]:
        """Active brands ordered by name (case-insensitive)."""
        ...

    async def list_all(self, *, page: int, size: int) -> AdminBrandPage:
        """Every brand ordered by name (case-insensitive), one page at a time."""
        ...
