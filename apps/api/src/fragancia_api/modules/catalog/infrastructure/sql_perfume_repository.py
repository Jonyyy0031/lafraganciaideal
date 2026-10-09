from typing import Any
from uuid import UUID

from psycopg import errors as pg_errors
from sqlalchemy import exists, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PresentationAlreadyExists,
)
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    Perfume,
    PerfumeName,
    Presentation,
    Price,
    Sale,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    PERFUME_IDENTITY_UNIQUE,
    PERFUME_SLUG_UNIQUE,
    PRESENTATION_ML_UNIQUE,
    perfume_slug_history,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Err, Money, Ok, Result


def _perfume_row(perfume: Perfume) -> dict[str, Any]:
    return {
        "id": perfume.id,
        "brand_id": perfume.brand_id,
        "concentration_id": perfume.concentration_id,
        "family_id": perfume.family_id,
        "name": perfume.name.value,
        "name_slug": perfume.name_slug,
        "slug": perfume.slug,
        "gender": perfume.gender.value,
        "description": perfume.description.value,
        "top_notes": list(perfume.notes.top),
        "heart_notes": list(perfume.notes.heart),
        "base_notes": list(perfume.notes.base),
        "is_published": perfume.is_published,
        "first_published_at": perfume.first_published_at,
        "is_archived": perfume.is_archived,
        "created_at": perfume.created_at,
        "updated_at": perfume.updated_at,
    }


def _presentation_row(perfume_id: UUID, presentation: Presentation) -> dict[str, Any]:
    sale = presentation.sale
    availability = presentation.availability
    return {
        "id": presentation.id,
        "perfume_id": perfume_id,
        "ml": presentation.ml.value,
        "price_cents": presentation.price.amount.cents,
        "sale_price_cents": sale.price.cents if sale is not None else None,
        "sale_starts_at": sale.starts_at if sale is not None else None,
        "sale_ends_at": sale.ends_at if sale is not None else None,
        "availability": availability.kind,
        "lead_time_min_days": availability.min_days,
        "lead_time_max_days": availability.max_days,
        "is_active": presentation.is_active,
        "created_at": presentation.created_at,
    }


# Every column a presentation upsert may change (never id, perfume_id or created_at).
_PRESENTATION_MUTABLE = (
    "ml",
    "price_cents",
    "sale_price_cents",
    "sale_starts_at",
    "sale_ends_at",
    "availability",
    "lead_time_min_days",
    "lead_time_max_days",
    "is_active",
)


def _to_presentation(row: Any) -> Presentation:
    # Values read from the database are already valid: built directly, without `create`.
    sale = (
        Sale(Money(row["sale_price_cents"]), row["sale_starts_at"], row["sale_ends_at"])
        if row["sale_price_cents"] is not None
        else None
    )
    return Presentation(
        id=row["id"],
        ml=Ml(row["ml"]),
        price=Price(Money(row["price_cents"])),
        sale=sale,
        availability=Availability(
            row["availability"], row["lead_time_min_days"], row["lead_time_max_days"]
        ),
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


def _to_perfume(row: Any, presentation_rows: Any) -> Perfume:
    # Values read from the database are already valid: built directly, without `create`.
    return Perfume(
        id=row["id"],
        brand_id=row["brand_id"],
        concentration_id=row["concentration_id"],
        family_id=row["family_id"],
        name=PerfumeName(row["name"]),
        slug=row["slug"],
        gender=Gender(row["gender"]),
        description=Description(row["description"]),
        notes=Notes(tuple(row["top_notes"]), tuple(row["heart_notes"]), tuple(row["base_notes"])),
        is_published=row["is_published"],
        first_published_at=row["first_published_at"],
        is_archived=row["is_archived"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        presentations=[_to_presentation(presentation) for presentation in presentation_rows],
    )


def _conflict(error: IntegrityError) -> PerfumeAlreadyExists | PresentationAlreadyExists:
    """The domain error for a known unique violation; anything else is re-raised."""
    if _violates(error, PERFUME_IDENTITY_UNIQUE) or _violates(error, PERFUME_SLUG_UNIQUE):
        return PerfumeAlreadyExists()
    if _violates(error, PRESENTATION_ML_UNIQUE):
        return PresentationAlreadyExists()
    raise error


class SqlPerfumeRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def exists_with_identity(
        self,
        brand_id: UUID,
        name_slug: str,
        concentration_id: UUID,
        *,
        except_id: UUID | None = None,
    ) -> bool:
        condition = (
            (perfumes.c.brand_id == brand_id)
            & (perfumes.c.name_slug == name_slug)
            & (perfumes.c.concentration_id == concentration_id)
        )
        if except_id is not None:
            condition = condition & (perfumes.c.id != except_id)
        return bool(await self._database.session.scalar(select(exists().where(condition))))

    async def add(self, perfume: Perfume) -> Result[None, PerfumeAlreadyExists]:
        session = self._database.session
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(insert(perfumes).values(_perfume_row(perfume)))
                for presentation in perfume.presentations:
                    await session.execute(
                        insert(presentations).values(_presentation_row(perfume.id, presentation))
                    )
        except IntegrityError as error:
            if _violates(error, PERFUME_IDENTITY_UNIQUE) or _violates(error, PERFUME_SLUG_UNIQUE):
                return Err(PerfumeAlreadyExists())
            raise
        return Ok(None)

    async def get_for_update(self, perfume_id: UUID) -> Perfume | None:
        session = self._database.session
        statement = select(perfumes).where(perfumes.c.id == perfume_id).with_for_update()
        row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        presentation_rows = (
            (
                await session.execute(
                    select(presentations)
                    .where(presentations.c.perfume_id == perfume_id)
                    .order_by(presentations.c.ml)
                )
            )
            .mappings()
            .all()
        )
        return _to_perfume(row, presentation_rows)

    async def save(
        self, perfume: Perfume
    ) -> Result[None, PerfumeAlreadyExists | PresentationAlreadyExists]:
        session = self._database.session
        values = _perfume_row(perfume)
        del values["id"], values["created_at"]
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(
                    update(perfumes).where(perfumes.c.id == perfume.id).values(values)
                )
                for presentation in perfume.presentations:
                    row = _presentation_row(perfume.id, presentation)
                    statement = postgresql.insert(presentations).values(row)
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["id"],
                            set_={column: row[column] for column in _PRESENTATION_MUTABLE},
                        )
                    )
                for slug in perfume.retired_slugs:
                    history = postgresql.insert(perfume_slug_history).values(
                        slug=slug, perfume_id=perfume.id, retired_at=perfume.updated_at
                    )
                    await session.execute(
                        history.on_conflict_do_update(
                            index_elements=["slug"],
                            set_={"perfume_id": perfume.id, "retired_at": perfume.updated_at},
                        )
                    )
        except IntegrityError as error:
            return Err(_conflict(error))
        perfume.retired_slugs.clear()
        return Ok(None)


def _violates(error: IntegrityError, constraint: str) -> bool:
    cause = error.orig
    return isinstance(cause, pg_errors.UniqueViolation) and cause.diag.constraint_name == constraint
