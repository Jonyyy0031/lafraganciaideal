from sqlalchemy import func, select

from fragancia_api.modules.catalog.contracts import (
    AdminConcentration,
    AdminConcentrationPage,
    PublicConcentration,
)
from fragancia_api.modules.catalog.infrastructure.tables import concentrations
from fragancia_api.shared.infrastructure.database import Database

_BY_NAME = (func.lower(concentrations.c.name), concentrations.c.id)


class SqlConcentrationQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_active(self) -> list[PublicConcentration]:
        statement = (
            select(
                concentrations.c.id,
                concentrations.c.name,
                concentrations.c.abbreviation,
                concentrations.c.slug,
            )
            .where(concentrations.c.is_active)
            .order_by(*_BY_NAME)
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings()
            return [PublicConcentration.model_validate(dict(row)) for row in rows]

    async def list_all(self, *, page: int, size: int) -> AdminConcentrationPage:
        statement = (
            select(
                concentrations.c.id,
                concentrations.c.name,
                concentrations.c.abbreviation,
                concentrations.c.slug,
                concentrations.c.is_active,
                concentrations.c.created_at,
            )
            .order_by(*_BY_NAME)
            .limit(size)
            .offset((page - 1) * size)
        )
        async with self._database.reader() as session:
            total = await session.scalar(select(func.count()).select_from(concentrations)) or 0
            rows = (await session.execute(statement)).mappings()
            items = [AdminConcentration.model_validate(dict(row)) for row in rows]
        return AdminConcentrationPage(items=items, total=total, page=page, size=size)
