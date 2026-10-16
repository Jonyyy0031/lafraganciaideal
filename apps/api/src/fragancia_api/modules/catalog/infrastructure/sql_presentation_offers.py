from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from fragancia_api.modules.catalog.application.queries.presentation_offers import (
    PresentationOffer,
)
from fragancia_api.modules.catalog.infrastructure.sql_perfume_queries import (
    _VISIBLE,
    _effective_price,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    perfume_photos,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database


class SqlPresentationOffers:
    def __init__(self, database: Database, *, media_base_url: str) -> None:
        self._database = database
        self._media_base_url = media_base_url

    def _offer(self, row: Any) -> PresentationOffer:
        in_stock = row["availability"] == "in_stock"
        card_key = row["card_key"]
        return PresentationOffer(
            presentation_id=row["presentation_id"],
            perfume_id=row["perfume_id"],
            perfume_slug=row["perfume_slug"],
            label=(
                f"{row['brand_name']} {row['perfume_name']} {row['abbreviation']} {row['ml']} ml"
            ),
            ml=row["ml"],
            card_url=f"{self._media_base_url}/{card_key}" if card_key is not None else None,
            price_cents=row["price_cents"],
            regular_price_cents=row["regular_price_cents"],
            availability=row["availability"],
            lead_time_min_days=row["lead_time_min_days"],
            lead_time_max_days=row["lead_time_max_days"],
            is_visible=bool(row["is_visible"]) and bool(row["is_active"]),
            available_units=row["stock_available"] if in_stock else None,
        )

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, PresentationOffer]:
        if not ids:
            return {}
        first_photo = (
            select(perfume_photos.c.card_key)
            .where(perfume_photos.c.perfume_id == perfumes.c.id, perfume_photos.c.position == 1)
            .correlate(perfumes)
            .scalar_subquery()
        )
        statement = (
            select(
                presentations.c.id.label("presentation_id"),
                perfumes.c.id.label("perfume_id"),
                perfumes.c.slug.label("perfume_slug"),
                perfumes.c.name.label("perfume_name"),
                brands.c.name.label("brand_name"),
                concentrations.c.abbreviation.label("abbreviation"),
                presentations.c.ml,
                first_photo.label("card_key"),
                _effective_price(now).label("price_cents"),
                presentations.c.price_cents.label("regular_price_cents"),
                presentations.c.availability,
                presentations.c.lead_time_min_days,
                presentations.c.lead_time_max_days,
                presentations.c.is_active,
                _VISIBLE.label("is_visible"),
                presentations.c.stock_available,
            )
            .select_from(presentations)
            .join(perfumes, presentations.c.perfume_id == perfumes.c.id)
            .join(brands, perfumes.c.brand_id == brands.c.id)
            .join(concentrations, perfumes.c.concentration_id == concentrations.c.id)
            .where(presentations.c.id.in_(list(ids)))
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return {offer.presentation_id: offer for offer in map(self._offer, rows)}
