from sqlalchemy import func, select

from fragancia_api.modules.catalog.contracts import (
    AdminOlfactoryFamily,
    AdminOlfactoryFamilyPage,
    PublicOlfactoryFamily,
)
from fragancia_api.modules.catalog.infrastructure.tables import olfactory_families
from fragancia_api.shared.infrastructure.database import Database

_BY_NAME = (func.lower(olfactory_families.c.name), olfactory_families.c.id)


class SqlOlfactoryFamilyQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_active(self) -> list[PublicOlfactoryFamily]:
        statement = (
            select(olfactory_families.c.id, olfactory_families.c.name, olfactory_families.c.slug)
            .where(olfactory_families.c.is_active)
            .order_by(*_BY_NAME)
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings()
            return [PublicOlfactoryFamily.model_validate(dict(row)) for row in rows]

    async def list_all(self, *, page: int, size: int) -> AdminOlfactoryFamilyPage:
        statement = (
            select(
                olfactory_families.c.id,
                olfactory_families.c.name,
                olfactory_families.c.slug,
                olfactory_families.c.is_active,
                olfactory_families.c.created_at,
            )
            .order_by(*_BY_NAME)
            .limit(size)
            .offset((page - 1) * size)
        )
        async with self._database.reader() as session:
            total = await session.scalar(select(func.count()).select_from(olfactory_families)) or 0
            rows = (await session.execute(statement)).mappings()
            items = [AdminOlfactoryFamily.model_validate(dict(row)) for row in rows]
        return AdminOlfactoryFamilyPage(items=items, total=total, page=page, size=size)
