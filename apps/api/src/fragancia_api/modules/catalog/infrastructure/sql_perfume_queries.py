from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    AdminPerfumeSummary,
    AdminPresentation,
    PerfumeBrandRef,
    PerfumeConcentrationRef,
    PerfumeFamilyRef,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database

_BRAND = (
    brands.c.id.label("brand_id"),
    brands.c.name.label("brand_name"),
    brands.c.is_active.label("brand_is_active"),
)
_CONCENTRATION = (
    concentrations.c.id.label("concentration_id"),
    concentrations.c.name.label("concentration_name"),
    concentrations.c.abbreviation.label("concentration_abbreviation"),
    concentrations.c.is_active.label("concentration_is_active"),
)
_BY_BRAND_THEN_NAME = (func.lower(brands.c.name), func.lower(perfumes.c.name), perfumes.c.id)


def _brand_ref(row: Any) -> PerfumeBrandRef:
    return PerfumeBrandRef(
        id=row["brand_id"], name=row["brand_name"], is_active=row["brand_is_active"]
    )


def _concentration_ref(row: Any) -> PerfumeConcentrationRef:
    return PerfumeConcentrationRef(
        id=row["concentration_id"],
        name=row["concentration_name"],
        abbreviation=row["concentration_abbreviation"],
        is_active=row["concentration_is_active"],
    )


class SqlPerfumeQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_admin(self, *, page: int, size: int, archived: bool) -> AdminPerfumePage:
        active_presentations = (
            select(func.count())
            .select_from(presentations)
            .where((presentations.c.perfume_id == perfumes.c.id) & presentations.c.is_active)
            .correlate(perfumes)
            .scalar_subquery()
        )
        statement = (
            select(
                perfumes.c.id,
                perfumes.c.slug,
                perfumes.c.name,
                perfumes.c.gender,
                perfumes.c.is_published,
                perfumes.c.is_archived,
                perfumes.c.created_at,
                active_presentations.label("active_presentations"),
                *_BRAND,
                *_CONCENTRATION,
            )
            .join_from(perfumes, brands, perfumes.c.brand_id == brands.c.id)
            .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
            .where(perfumes.c.is_archived == archived)
            .order_by(*_BY_BRAND_THEN_NAME)
            .limit(size)
            .offset((page - 1) * size)
        )
        count = select(func.count()).select_from(perfumes).where(perfumes.c.is_archived == archived)
        async with self._database.reader() as session:
            total = await session.scalar(count) or 0
            rows = (await session.execute(statement)).mappings().all()
        items = [
            AdminPerfumeSummary(
                id=row["id"],
                slug=row["slug"],
                name=row["name"],
                brand=_brand_ref(row),
                concentration=_concentration_ref(row),
                gender=row["gender"],
                is_published=row["is_published"],
                is_archived=row["is_archived"],
                active_presentations=row["active_presentations"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
        return AdminPerfumePage(items=items, total=total, page=page, size=size)

    async def get_admin(self, perfume_id: UUID) -> AdminPerfume | None:
        statement = (
            select(
                # brand_id and concentration_id come labeled with their refs below.
                *(c for c in perfumes.c if c.name not in ("brand_id", "concentration_id")),
                *_BRAND,
                *_CONCENTRATION,
                olfactory_families.c.name.label("family_name"),
                olfactory_families.c.is_active.label("family_is_active"),
            )
            .join_from(perfumes, brands, perfumes.c.brand_id == brands.c.id)
            .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
            .join(olfactory_families, perfumes.c.family_id == olfactory_families.c.id)
            .where(perfumes.c.id == perfume_id)
        )
        presentation_statement = (
            select(presentations)
            .where(presentations.c.perfume_id == perfume_id)
            .order_by(presentations.c.ml)
        )
        async with self._database.reader() as session:
            row = (await session.execute(statement)).mappings().first()
            if row is None:
                return None
            presentation_rows = (await session.execute(presentation_statement)).mappings().all()
        return AdminPerfume(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            gender=row["gender"],
            description=row["description"],
            top_notes=list(row["top_notes"]),
            heart_notes=list(row["heart_notes"]),
            base_notes=list(row["base_notes"]),
            brand=_brand_ref(row),
            concentration=_concentration_ref(row),
            family=PerfumeFamilyRef(
                id=row["family_id"], name=row["family_name"], is_active=row["family_is_active"]
            ),
            is_published=row["is_published"],
            first_published_at=row["first_published_at"],
            is_archived=row["is_archived"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            presentations=[AdminPresentation.model_validate(dict(p)) for p in presentation_rows],
        )
