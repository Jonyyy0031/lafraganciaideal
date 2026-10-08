"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from uuid import UUID

from fragancia_api.modules.catalog.contracts import (
    AdminBrand,
    AdminBrandPage,
    AdminOlfactoryFamily,
    AdminOlfactoryFamilyPage,
    PublicBrand,
    PublicOlfactoryFamily,
)
from fragancia_api.modules.catalog.domain.brand import Brand
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists, FamilyAlreadyExists
from fragancia_api.modules.catalog.domain.olfactory_family import OlfactoryFamily
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryBrands:
    """Both the repository and the queries over one shared store."""

    def __init__(self, *brands: Brand) -> None:
        self.by_id: dict[UUID, Brand] = {brand.id: brand for brand in brands}

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        return any(brand.slug == slug and brand.id != except_id for brand in self.by_id.values())

    async def add(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        if await self.exists_with_slug(brand.slug):
            return Err(BrandAlreadyExists())
        self.by_id[brand.id] = brand
        return Ok(None)

    async def get_for_update(self, brand_id: UUID) -> Brand | None:
        return self.by_id.get(brand_id)

    async def save(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        if await self.exists_with_slug(brand.slug, except_id=brand.id):
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

    async def list_all(self, *, page: int, size: int) -> AdminBrandPage:
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
        return AdminBrandPage(items=items, total=len(everything), page=page, size=size)


class InMemoryOlfactoryFamilies:
    """Both the repository and the queries over one shared store."""

    def __init__(self, *families: OlfactoryFamily) -> None:
        self.by_id: dict[UUID, OlfactoryFamily] = {family.id: family for family in families}

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        return any(family.slug == slug and family.id != except_id for family in self.by_id.values())

    async def add(self, family: OlfactoryFamily) -> Result[None, FamilyAlreadyExists]:
        if await self.exists_with_slug(family.slug):
            return Err(FamilyAlreadyExists())
        self.by_id[family.id] = family
        return Ok(None)

    async def get_for_update(self, family_id: UUID) -> OlfactoryFamily | None:
        return self.by_id.get(family_id)

    async def save(self, family: OlfactoryFamily) -> Result[None, FamilyAlreadyExists]:
        if await self.exists_with_slug(family.slug, except_id=family.id):
            return Err(FamilyAlreadyExists())
        self.by_id[family.id] = family
        return Ok(None)

    def _sorted(self) -> list[OlfactoryFamily]:
        return sorted(self.by_id.values(), key=lambda f: (f.name.value.lower(), f.id))

    async def list_active(self) -> list[PublicOlfactoryFamily]:
        return [
            PublicOlfactoryFamily(id=f.id, name=f.name.value, slug=f.slug)
            for f in self._sorted()
            if f.is_active
        ]

    async def list_all(self, *, page: int, size: int) -> AdminOlfactoryFamilyPage:
        everything = self._sorted()
        chunk = everything[(page - 1) * size : page * size]
        items = [
            AdminOlfactoryFamily(
                id=f.id,
                name=f.name.value,
                slug=f.slug,
                is_active=f.is_active,
                created_at=f.created_at,
            )
            for f in chunk
        ]
        return AdminOlfactoryFamilyPage(items=items, total=len(everything), page=page, size=size)
