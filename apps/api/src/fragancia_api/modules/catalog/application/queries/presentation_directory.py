"""Public facade of the catalog for other modules: look presentations up by id."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    from fragancia_api.modules.catalog.application.ports import PresentationDirectoryQueries


@dataclass(frozen=True, slots=True)
class PresentationRef:
    """A presentation with the names that identify it.

    `is_sellable` is true when the presentation is active, its perfume is not archived and the
    perfume's brand is active.
    """

    presentation_id: UUID
    perfume_id: UUID
    perfume_name: str
    brand_name: str
    concentration_abbreviation: str
    ml: int
    availability: Literal["in_stock", "made_to_order"]
    is_sellable: bool


class PresentationDirectory:
    """Query facade: the only way other modules read presentations from the catalog."""

    def __init__(self, queries: PresentationDirectoryQueries) -> None:
        self._queries = queries

    async def find(self, ids: Sequence[UUID]) -> dict[UUID, PresentationRef]:
        """The known presentations by id; unknown ids are absent."""
        return await self._queries.find(ids)

    async def list_in_stock(self) -> list[PresentationRef]:
        """Every active in-stock presentation of a non-archived perfume, ordered by brand
        name, perfume name and ml."""
        return await self._queries.list_in_stock()
