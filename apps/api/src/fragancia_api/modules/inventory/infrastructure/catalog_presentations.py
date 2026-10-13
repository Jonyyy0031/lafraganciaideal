"""Anti-corruption layer: inventory's `PresentationCatalog` over the catalog's public facade."""

from collections.abc import Sequence
from uuid import UUID

from fragancia_api.modules.catalog import PresentationDirectory, PresentationRef
from fragancia_api.modules.inventory.application.ports import StockedPresentation


def _stocked(ref: PresentationRef) -> StockedPresentation:
    label = f"{ref.brand_name} {ref.perfume_name} {ref.concentration_abbreviation} {ref.ml} ml"
    return StockedPresentation(
        presentation_id=ref.presentation_id,
        label=label,
        perfume_id=ref.perfume_id,
        stocked=ref.availability == "in_stock",
        is_sellable=ref.is_sellable,
    )


class CatalogPresentations:
    def __init__(self, directory: PresentationDirectory) -> None:
        self._directory = directory

    async def describe(self, ids: Sequence[UUID]) -> dict[UUID, StockedPresentation]:
        found = await self._directory.find(ids)
        return {presentation_id: _stocked(ref) for presentation_id, ref in found.items()}

    async def describe_for_update(self, presentation_id: UUID) -> StockedPresentation | None:
        ref = await self._directory.find_for_update(presentation_id)
        return _stocked(ref) if ref is not None else None

    async def in_stock(self) -> list[StockedPresentation]:
        return [_stocked(ref) for ref in await self._directory.list_in_stock()]
