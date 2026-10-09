from typing import Any
from uuid import UUID

from psycopg import errors as pg_errors
from sqlalchemy import exists, insert, select, update
from sqlalchemy.exc import IntegrityError

from fragancia_api.modules.catalog.domain.errors import FamilyAlreadyExists
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName, OlfactoryFamily
from fragancia_api.modules.catalog.infrastructure.tables import (
    FAMILY_SLUG_UNIQUE,
    olfactory_families,
)
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Err, Ok, Result


def _to_row(family: OlfactoryFamily) -> dict[str, Any]:
    return {
        "id": family.id,
        "name": family.name.value,
        "slug": family.slug,
        "is_active": family.is_active,
        "created_at": family.created_at,
    }


def _to_family(row: Any) -> OlfactoryFamily:
    return OlfactoryFamily(
        id=row["id"],
        name=FamilyName(row["name"]),  # already clean in the database
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


class SqlOlfactoryFamilyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        condition = olfactory_families.c.slug == slug
        if except_id is not None:
            condition = condition & (olfactory_families.c.id != except_id)
        return bool(await self._database.session.scalar(select(exists().where(condition))))

    async def add(self, family: OlfactoryFamily) -> Result[None, FamilyAlreadyExists]:
        session = self._database.session
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(insert(olfactory_families).values(_to_row(family)))
        except IntegrityError as error:
            if _violates(error, FAMILY_SLUG_UNIQUE):
                return Err(FamilyAlreadyExists())
            raise
        return Ok(None)

    async def get(self, family_id: UUID) -> OlfactoryFamily | None:
        statement = select(olfactory_families).where(olfactory_families.c.id == family_id)
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_family(row) if row is not None else None

    async def get_for_update(self, family_id: UUID) -> OlfactoryFamily | None:
        statement = (
            select(olfactory_families).where(olfactory_families.c.id == family_id).with_for_update()
        )
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_family(row) if row is not None else None

    async def save(self, family: OlfactoryFamily) -> Result[None, FamilyAlreadyExists]:
        session = self._database.session
        statement = (
            update(olfactory_families)
            .where(olfactory_families.c.id == family.id)
            .values(name=family.name.value, slug=family.slug, is_active=family.is_active)
        )
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(statement)
        except IntegrityError as error:
            if _violates(error, FAMILY_SLUG_UNIQUE):
                return Err(FamilyAlreadyExists())
            raise
        return Ok(None)


def _violates(error: IntegrityError, constraint: str) -> bool:
    cause = error.orig
    return isinstance(cause, pg_errors.UniqueViolation) and cause.diag.constraint_name == constraint
