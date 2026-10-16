"""Anti-corruption layer: orders' `CatalogOffers` over the catalog's public facade."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.catalog import PresentationOffer, PresentationOffers
from fragancia_api.modules.orders.domain.cart import Offer


def _offer(source: PresentationOffer) -> Offer:
    return Offer(
        presentation_id=source.presentation_id,
        perfume_id=source.perfume_id,
        perfume_slug=source.perfume_slug,
        label=source.label,
        ml=source.ml,
        card_url=source.card_url,
        price_cents=source.price_cents,
        regular_price_cents=source.regular_price_cents,
        availability=source.availability,
        lead_time_min_days=source.lead_time_min_days,
        lead_time_max_days=source.lead_time_max_days,
        is_visible=source.is_visible,
        available_units=source.available_units,
    )


class CatalogOffersAdapter:
    def __init__(self, offers: PresentationOffers) -> None:
        self._offers = offers

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, Offer]:
        found = await self._offers.offers(ids, now)
        return {presentation_id: _offer(offer) for presentation_id, offer in found.items()}
