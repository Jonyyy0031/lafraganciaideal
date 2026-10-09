from typing import Any
from uuid import UUID

from psycopg import errors as pg_errors
from sqlalchemy import exists, insert, select, update
from sqlalchemy.exc import IntegrityError

from fragancia_api.modules.catalog.domain.concentration import (
    Abbreviation,
    Concentration,
    ConcentrationName,
)
from fragancia_api.modules.catalog.domain.errors import (
    ConcentrationAbbreviationTaken,
    ConcentrationAlreadyExists,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    CONCENTRATION_ABBREVIATION_UNIQUE,
    CONCENTRATION_SLUG_UNIQUE,
    concentrations,
)
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Err, Ok, Result


def _to_row(concentration: Concentration) -> dict[str, Any]:
    return {
        "id": concentration.id,
        "name": concentration.name.value,
        "slug": concentration.slug,
        "abbreviation": concentration.abbreviation.value,
        "abbreviation_slug": concentration.abbreviation_slug,
        "is_active": concentration.is_active,
        "created_at": concentration.created_at,
    }


def _to_concentration(row: Any) -> Concentration:
    return Concentration(
        id=row["id"],
        name=ConcentrationName(row["name"]),  # already clean in the database
        abbreviation=Abbreviation(row["abbreviation"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


def _conflict(error: IntegrityError) -> ConcentrationAlreadyExists | ConcentrationAbbreviationTaken:
    """The domain error for a known unique violation; anything else is re-raised."""
    if _violates(error, CONCENTRATION_SLUG_UNIQUE):
        return ConcentrationAlreadyExists()
    if _violates(error, CONCENTRATION_ABBREVIATION_UNIQUE):
        return ConcentrationAbbreviationTaken()
    raise error


class SqlConcentrationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        condition = concentrations.c.slug == slug
        if except_id is not None:
            condition = condition & (concentrations.c.id != except_id)
        return bool(await self._database.session.scalar(select(exists().where(condition))))

    async def exists_with_abbreviation(
        self, abbreviation_slug: str, *, except_id: UUID | None = None
    ) -> bool:
        condition = concentrations.c.abbreviation_slug == abbreviation_slug
        if except_id is not None:
            condition = condition & (concentrations.c.id != except_id)
        return bool(await self._database.session.scalar(select(exists().where(condition))))

    async def add(
        self, concentration: Concentration
    ) -> Result[None, ConcentrationAlreadyExists | ConcentrationAbbreviationTaken]:
        session = self._database.session
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(insert(concentrations).values(_to_row(concentration)))
        except IntegrityError as error:
            return Err(_conflict(error))
        return Ok(None)

    async def get_for_update(self, concentration_id: UUID) -> Concentration | None:
        statement = (
            select(concentrations).where(concentrations.c.id == concentration_id).with_for_update()
        )
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_concentration(row) if row is not None else None

    async def save(
        self, concentration: Concentration
    ) -> Result[None, ConcentrationAlreadyExists | ConcentrationAbbreviationTaken]:
        session = self._database.session
        statement = (
            update(concentrations)
            .where(concentrations.c.id == concentration.id)
            .values(
                name=concentration.name.value,
                slug=concentration.slug,
                abbreviation=concentration.abbreviation.value,
                abbreviation_slug=concentration.abbreviation_slug,
                is_active=concentration.is_active,
            )
        )
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(statement)
        except IntegrityError as error:
            return Err(_conflict(error))
        return Ok(None)


def _violates(error: IntegrityError, constraint: str) -> bool:
    cause = error.orig
    return isinstance(cause, pg_errors.UniqueViolation) and cause.diag.constraint_name == constraint
