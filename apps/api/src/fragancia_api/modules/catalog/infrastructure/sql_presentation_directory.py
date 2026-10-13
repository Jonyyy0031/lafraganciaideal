from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, func, select

from fragancia_api.modules.catalog.application.queries.presentation_directory import (
    PresentationRef,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database


def _select() -> Select[Any]:
    return (
        select(
            presentations.c.id.label("presentation_id"),
            perfumes.c.id.label("perfume_id"),
            perfumes.c.name.label("perfume_name"),
            brands.c.name.label("brand_name"),
            concentrations.c.abbreviation.label("abbreviation"),
            presentations.c.ml,
            presentations.c.availability,
            (presentations.c.is_active & ~perfumes.c.is_archived & brands.c.is_active).label(
                "is_sellable"
            ),
        )
        .select_from(presentations)
        .join(perfumes, presentations.c.perfume_id == perfumes.c.id)
        .join(brands, perfumes.c.brand_id == brands.c.id)
        .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
    )


def _ref(row: Any) -> PresentationRef:
    return PresentationRef(
        presentation_id=row["presentation_id"],
        perfume_id=row["perfume_id"],
        perfume_name=row["perfume_name"],
        brand_name=row["brand_name"],
        concentration_abbreviation=row["abbreviation"],
        ml=row["ml"],
        availability=row["availability"],
        is_sellable=bool(row["is_sellable"]),
    )


class SqlPresentationDirectory:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def find(self, ids: Sequence[UUID]) -> dict[UUID, PresentationRef]:
        if not ids:
            return {}
        statement = _select().where(presentations.c.id.in_(list(ids)))
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return {ref.presentation_id: ref for ref in map(_ref, rows)}

    async def list_in_stock(self) -> list[PresentationRef]:
        conditions: list[ColumnElement[bool]] = [
            presentations.c.is_active,
            presentations.c.availability == "in_stock",
            ~perfumes.c.is_archived,
        ]
        statement = (
            _select()
            .where(*conditions)
            .order_by(
                func.lower(brands.c.name),
                func.lower(perfumes.c.name),
                presentations.c.ml,
                presentations.c.id,
            )
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [_ref(row) for row in rows]
