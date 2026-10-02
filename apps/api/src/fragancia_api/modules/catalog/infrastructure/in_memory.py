"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from fragancia_api.modules.catalog.contracts import AdminBrand, PublicBrand
from fragancia_api.modules.catalog.domain.brand import Brand
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists
from fragancia_api.shared.contracts import Page
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryBrands:
    """Both the repository and the queries over one shared store."""

    def __init__(self, *brands: Brand) -> None:
        self.by_id: dict[object, Brand] = {brand.id: brand for brand in brands}

    async def exists_with_slug(self, slug: str) -> bool:
        return any(brand.slug == slug for brand in self.by_id.values())

    async def add(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        if await self.exists_with_slug(brand.slug):
            return Err(BrandAlreadyExists())
        self.by_id[brand.id] = brand
        return Ok(None)

    def _sorted(self) -> list[Brand]:
        return sorted(self.by_id.values(), key=lambda b: (b.name.value.lower(), b.id))

    async def list_active(self) -> list[PublicBrand]:
        return [
            PublicBrand(id=b.id, name=b.name.value, slug=b.slug)
            for b in self._sorted()
            if b.is_active
        ]

    async def list_all(self, *, page: int, size: int) -> Page[AdminBrand]:
        everything = self._sorted()
        chunk = everything[(page - 1) * size : page * size]
        items = [
            AdminBrand(
                id=b.id,
                name=b.name.value,
                slug=b.slug,
                is_active=b.is_active,
                created_at=b.created_at,
            )
            for b in chunk
        ]
        return Page[AdminBrand](items=items, total=len(everything), page=page, size=size)
