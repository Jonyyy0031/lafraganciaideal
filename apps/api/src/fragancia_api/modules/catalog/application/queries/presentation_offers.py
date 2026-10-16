"""Public facade of the catalog for other modules: what a buyer needs to see of a presentation
(price, availability, photo), read fresh on every call."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    from fragancia_api.modules.catalog.application.ports import PresentationOffersQueries


@dataclass(frozen=True, slots=True)
class PresentationOffer:
    """A presentation as offered to a buyer at one moment.

    `price_cents` is the effective price (the sale price while the sale is active) and
    `regular_price_cents` the regular one. `is_visible` is true when the presentation is active,
    its perfume is published and not archived, and its brand is active. `available_units` is the
    stock copy's figure for in-stock presentations and None for made-to-order ones.
    """

    presentation_id: UUID
    perfume_id: UUID
    perfume_slug: str
    label: str
    ml: int
    card_url: str | None
    price_cents: int
    regular_price_cents: int
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    is_visible: bool
    available_units: int | None


class PresentationOffers:
    """Query facade: how other modules read prices and availability from the catalog."""

    def __init__(self, queries: PresentationOffersQueries) -> None:
        self._queries = queries

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, PresentationOffer]:
        """The known presentations by id, priced at `now`; unknown ids are absent.

        `available_units` comes from the catalog's stock copy, which lags inventory by about
        2 s: it is good for display, not for reserving.
        """
        return await self._queries.offers(ids, now)
