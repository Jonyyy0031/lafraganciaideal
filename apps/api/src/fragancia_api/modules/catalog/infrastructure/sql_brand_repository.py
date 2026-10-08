from typing import Any
from uuid import UUID

from psycopg import errors as pg_errors
from sqlalchemy import exists, insert, select, update
from sqlalchemy.exc import IntegrityError

from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
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


def _to_brand(row: Any) -> Brand:
    return Brand(
        id=row["id"],
        name=BrandName(row["name"]),  # already clean in the database
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


class SqlBrandRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        condition = brands.c.slug == slug
        if except_id is not None:
            condition = condition & (brands.c.id != except_id)
        return bool(await self._database.session.scalar(select(exists().where(condition))))

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

    async def get_for_update(self, brand_id: UUID) -> Brand | None:
        statement = select(brands).where(brands.c.id == brand_id).with_for_update()
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_brand(row) if row is not None else None

    async def save(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        session = self._database.session
        statement = (
            update(brands)
            .where(brands.c.id == brand.id)
            .values(name=brand.name.value, slug=brand.slug, is_active=brand.is_active)
        )
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(statement)
        except IntegrityError as error:
            if _violates(error, BRAND_SLUG_UNIQUE):
                return Err(BrandAlreadyExists())
            raise
        return Ok(None)


def _violates(error: IntegrityError, constraint: str) -> bool:
    cause = error.orig
    return isinstance(cause, pg_errors.UniqueViolation) and cause.diag.constraint_name == constraint
