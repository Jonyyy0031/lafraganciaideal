from typing import Any

from psycopg import errors as pg_errors
from sqlalchemy import exists, insert, select
from sqlalchemy.exc import IntegrityError

from fragancia_api.modules.catalog.domain.brand import Brand
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists
from fragancia_api.modules.catalog.infrastructure.tables import BRAND_SLUG_UNIQUE, brands
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Err, Ok, Result


def _to_row(brand: Brand) -> dict[str, Any]:
    return {
        "id": brand.id,
        "name": brand.name.value,
        "slug": brand.slug,
        "is_active": brand.is_active,
        "created_at": brand.created_at,
    }


class SqlBrandRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def exists_with_slug(self, slug: str) -> bool:
        statement = select(exists().where(brands.c.slug == slug))
        return bool(await self._database.session.scalar(statement))

    async def add(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        session = self._database.session
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(insert(brands).values(_to_row(brand)))
        except IntegrityError as error:
            if _violates(error, BRAND_SLUG_UNIQUE):
                return Err(BrandAlreadyExists())
            raise
        return Ok(None)


def _violates(error: IntegrityError, constraint: str) -> bool:
    cause = error.orig
    return isinstance(cause, pg_errors.UniqueViolation) and cause.diag.constraint_name == constraint
