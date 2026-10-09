from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, or_, select

from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    AdminPerfumeSummary,
    AdminPresentation,
    PerfumeBrandRef,
    PerfumeConcentrationRef,
    PerfumeFamilyRef,
    PublicPerfume,
    PublicPerfumeBrand,
    PublicPerfumeCard,
    PublicPerfumeConcentration,
    PublicPerfumeFamily,
    PublicPerfumePage,
    PublicPresentation,
)
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Ml,
    Presentation,
    Price,
    Sale,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfume_slug_history,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Money

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


def _sale_active(now: datetime) -> ColumnElement[bool]:
    """SQL mirror of `Sale.is_active` (a sale exists when its price is set)."""
    return and_(
        presentations.c.sale_price_cents.is_not(None),
        or_(presentations.c.sale_starts_at.is_(None), presentations.c.sale_starts_at <= now),
        or_(presentations.c.sale_ends_at.is_(None), now < presentations.c.sale_ends_at),
    )


def _effective_price(now: datetime) -> ColumnElement[int]:
    """SQL mirror of `Presentation.effective_price`."""
    return case(
        (_sale_active(now), presentations.c.sale_price_cents),
        else_=presentations.c.price_cents,
    )


_VISIBLE = perfumes.c.is_published & ~perfumes.c.is_archived & brands.c.is_active

_PUBLIC_ORDER = {
    "price_asc": lambda price_from: (price_from, func.lower(perfumes.c.name), perfumes.c.id),
    "price_desc": lambda price_from: (
        price_from.desc(),
        func.lower(perfumes.c.name),
        perfumes.c.id,
    ),
}


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _public_presentation(row: Any, now: datetime) -> PublicPresentation:
    presentation = Presentation(
        id=row["id"],
        ml=Ml(row["ml"]),
        price=Price(Money(row["price_cents"])),
        sale=(
            Sale(Money(row["sale_price_cents"]), row["sale_starts_at"], row["sale_ends_at"])
            if row["sale_price_cents"] is not None
            else None
        ),
        availability=Availability(
            row["availability"], row["lead_time_min_days"], row["lead_time_max_days"]
        ),
        is_active=row["is_active"],
        created_at=row["created_at"],
    )
    sale = presentation.sale
    on_sale = sale is not None and sale.is_active(now)
    return PublicPresentation(
        id=presentation.id,
        ml=presentation.ml.value,
        availability=presentation.availability.kind,
        lead_time_min_days=presentation.availability.min_days,
        lead_time_max_days=presentation.availability.max_days,
        price_cents=presentation.effective_price(now).cents,
        regular_price_cents=presentation.price.amount.cents if on_sale else None,
        sale_ends_at=sale.ends_at if sale is not None and on_sale else None,
    )


class SqlPerfumeQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_public(
        self, filters: PublicPerfumeFilters, *, now: datetime
    ) -> PublicPerfumePage:
        prices = (
            select(
                presentations.c.perfume_id.label("perfume_id"),
                func.min(_effective_price(now)).label("price_from"),
                func.bool_or(_sale_active(now)).label("on_sale"),
            )
            .where(presentations.c.is_active)
            .group_by(presentations.c.perfume_id)
            .subquery("prices")
        )
        price_from = prices.c.price_from
        conditions: list[ColumnElement[bool]] = [_VISIBLE]
        if filters.brands:
            conditions.append(brands.c.slug.in_(filters.brands))
        if filters.families:
            conditions.append(olfactory_families.c.slug.in_(filters.families))
        if filters.genders:
            conditions.append(perfumes.c.gender.in_(filters.genders))
        if filters.min_price_cents is not None:
            conditions.append(price_from >= filters.min_price_cents)
        if filters.max_price_cents is not None:
            conditions.append(price_from <= filters.max_price_cents)
        if filters.q is not None:
            haystack = func.concat_ws(
                " ",
                perfumes.c.name,
                brands.c.name,
                func.array_to_string(perfumes.c.top_notes, " "),
                func.array_to_string(perfumes.c.heart_notes, " "),
                func.array_to_string(perfumes.c.base_notes, " "),
            )
            pattern = _like_pattern(filters.q)
            conditions.append(
                func.unaccent(func.lower(haystack)).like(
                    func.unaccent(func.lower(pattern)), escape="\\"
                )
            )
        order: tuple[ColumnElement[Any], ...]
        if filters.sort == "name":
            order = _BY_BRAND_THEN_NAME
        elif filters.sort == "newest":
            order = (perfumes.c.first_published_at.desc().nulls_last(), perfumes.c.id)
        else:
            order = _PUBLIC_ORDER[filters.sort](price_from)

        base = (
            select(perfumes.c.id)
            .join_from(perfumes, brands, perfumes.c.brand_id == brands.c.id)
            .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
            .join(olfactory_families, perfumes.c.family_id == olfactory_families.c.id)
            .join(prices, prices.c.perfume_id == perfumes.c.id)
            .where(*conditions)
        )
        statement = (
            select(
                perfumes.c.slug,
                perfumes.c.name,
                perfumes.c.gender,
                brands.c.name.label("brand_name"),
                brands.c.slug.label("brand_slug"),
                concentrations.c.name.label("concentration_name"),
                concentrations.c.abbreviation.label("concentration_abbreviation"),
                olfactory_families.c.name.label("family_name"),
                olfactory_families.c.slug.label("family_slug"),
                price_from.label("price_from"),
                prices.c.on_sale.label("on_sale"),
            )
            .join_from(perfumes, brands, perfumes.c.brand_id == brands.c.id)
            .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
            .join(olfactory_families, perfumes.c.family_id == olfactory_families.c.id)
            .join(prices, prices.c.perfume_id == perfumes.c.id)
            .where(*conditions)
            .order_by(*order)
            .limit(filters.size)
            .offset((filters.page - 1) * filters.size)
        )
        count = select(func.count()).select_from(base.subquery())
        async with self._database.reader() as session:
            total = await session.scalar(count) or 0
            rows = (await session.execute(statement)).mappings().all()
        items = [
            PublicPerfumeCard(
                slug=row["slug"],
                name=row["name"],
                gender=row["gender"],
                brand=PublicPerfumeBrand(name=row["brand_name"], slug=row["brand_slug"]),
                concentration=PublicPerfumeConcentration(
                    name=row["concentration_name"], abbreviation=row["concentration_abbreviation"]
                ),
                family=PublicPerfumeFamily(name=row["family_name"], slug=row["family_slug"]),
                price_from_cents=row["price_from"],
                on_sale=row["on_sale"],
            )
            for row in rows
        ]
        return PublicPerfumePage(items=items, total=total, page=filters.page, size=filters.size)

    async def get_public(self, slug: str, *, now: datetime) -> PublicPerfume | None:
        def find(*where: ColumnElement[bool]) -> Any:
            return (
                select(
                    perfumes.c.id,
                    perfumes.c.slug,
                    perfumes.c.name,
                    perfumes.c.gender,
                    perfumes.c.description,
                    perfumes.c.top_notes,
                    perfumes.c.heart_notes,
                    perfumes.c.base_notes,
                    brands.c.name.label("brand_name"),
                    brands.c.slug.label("brand_slug"),
                    concentrations.c.name.label("concentration_name"),
                    concentrations.c.abbreviation.label("concentration_abbreviation"),
                    olfactory_families.c.name.label("family_name"),
                    olfactory_families.c.slug.label("family_slug"),
                )
                .join_from(perfumes, brands, perfumes.c.brand_id == brands.c.id)
                .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
                .join(olfactory_families, perfumes.c.family_id == olfactory_families.c.id)
                .where(_VISIBLE, *where)
            )

        by_current = find(perfumes.c.slug == slug)
        by_history = (
            find()
            .join(perfume_slug_history, perfume_slug_history.c.perfume_id == perfumes.c.id)
            .where(perfume_slug_history.c.slug == slug)
        )
        async with self._database.reader() as session:
            row = (await session.execute(by_current)).mappings().first()
            if row is None:
                row = (await session.execute(by_history)).mappings().first()
            if row is None:
                return None
            presentation_rows = (
                (
                    await session.execute(
                        select(presentations)
                        .where(presentations.c.perfume_id == row["id"], presentations.c.is_active)
                        .order_by(presentations.c.ml)
                    )
                )
                .mappings()
                .all()
            )
        return PublicPerfume(
            slug=row["slug"],
            name=row["name"],
            gender=row["gender"],
            description=row["description"],
            brand=PublicPerfumeBrand(name=row["brand_name"], slug=row["brand_slug"]),
            concentration=PublicPerfumeConcentration(
                name=row["concentration_name"], abbreviation=row["concentration_abbreviation"]
            ),
            family=PublicPerfumeFamily(name=row["family_name"], slug=row["family_slug"]),
            top_notes=list(row["top_notes"]),
            heart_notes=list(row["heart_notes"]),
            base_notes=list(row["base_notes"]),
            presentations=[_public_presentation(p, now) for p in presentation_rows],
        )

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
