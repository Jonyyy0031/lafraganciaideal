from sqlalchemy import func, select

from fragancia_api.modules.catalog.contracts import AdminBrand, AdminBrandPage, PublicBrand
from fragancia_api.modules.catalog.infrastructure.tables import brands
from fragancia_api.shared.infrastructure.database import Database

_BY_NAME = (func.lower(brands.c.name), brands.c.id)


class SqlBrandQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_active(self) -> list[PublicBrand]:
        statement = (
            select(brands.c.id, brands.c.name, brands.c.slug)
            .where(brands.c.is_active)
            .order_by(*_BY_NAME)
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings()
            return [PublicBrand.model_validate(dict(row)) for row in rows]

    async def list_all(self, *, page: int, size: int) -> AdminBrandPage:
        statement = (
            select(
                brands.c.id, brands.c.name, brands.c.slug, brands.c.is_active, brands.c.created_at
            )
            .order_by(*_BY_NAME)
            .limit(size)
            .offset((page - 1) * size)
        )
        async with self._database.reader() as session:
            total = await session.scalar(select(func.count()).select_from(brands)) or 0
            rows = (await session.execute(statement)).mappings()
            items = [AdminBrand.model_validate(dict(row)) for row in rows]
        return AdminBrandPage(items=items, total=total, page=page, size=size)
