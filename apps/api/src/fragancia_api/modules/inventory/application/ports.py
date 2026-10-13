from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.inventory.contracts import StockMovementPage


@dataclass(frozen=True, slots=True)
class StockedPresentation:
    """A presentation as inventory needs it: how to name it and whether it carries units."""

    presentation_id: UUID
    label: str
    perfume_id: UUID
    stocked: bool
    is_sellable: bool


class PresentationCatalog(Protocol):
    """Inventory's view of the catalog, in inventory's terms (implemented by an adapter)."""

    async def describe(self, ids: Sequence[UUID]) -> dict[UUID, StockedPresentation]:
        """The known presentations by id; unknown ids are absent."""
        ...

    async def describe_for_update(self, presentation_id: UUID) -> StockedPresentation | None:
        """One presentation under a share lock held until the active transaction ends (so a
        concurrent catalog change waits); None when unknown. Run it inside a transaction."""
        ...

    async def in_stock(self) -> list[StockedPresentation]:
        """Every active in-stock presentation of a non-archived perfume, in display order."""
        ...


class StockQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def get(self, presentation_id: UUID) -> tuple[int, int | None] | None:
        """`(on_hand, threshold override)`, or None when the presentation was never counted."""
        ...

    async def movements(self, presentation_id: UUID, *, page: int, size: int) -> StockMovementPage:
        """The ledger of one presentation, newest first (ties by id), one page at a time."""
        ...

    async def on_hand_by_id(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int | None]]:
        """`(on_hand, threshold override)` of the counted presentations; others are absent."""
        ...

    async def low_stock_threshold(self) -> int:
        """The global low-stock threshold."""
        ...
