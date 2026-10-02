from typing import Protocol

from fragancia_api.modules.catalog.domain.brand import Brand
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists
from fragancia_api.shared.kernel import Result


class BrandRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see BrandQueries."""

    async def exists_with_slug(self, slug: str) -> bool: ...

    async def add(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        """Err when another brand already has the slug (also under concurrent inserts)."""
        ...
