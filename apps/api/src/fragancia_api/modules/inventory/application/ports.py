from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.inventory.contracts import ReservationHoldPage, StockMovementPage


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


@dataclass(frozen=True, slots=True)
class Recipient:
    """Someone who gets the low-stock email."""

    name: str
    email: str


class OwnerRecipients(Protocol):
    """Inventory's view of the owners, in inventory's terms (implemented by an adapter)."""

    async def active_owners(self) -> list[Recipient]:
        """The active owners, ordered by name."""
        ...


class StockQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def get(self, presentation_id: UUID) -> tuple[int, int, int | None] | None:
        """`(on_hand, reserved, threshold override)`, or None when the presentation was never
        counted."""
        ...

    async def movements(self, presentation_id: UUID, *, page: int, size: int) -> StockMovementPage:
        """The ledger of one presentation, newest first (ties by id), one page at a time."""
        ...

    async def on_hand_by_id(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, int | None]]:
        """`(on_hand, reserved, threshold override)` of the counted presentations; others are
        absent."""
        ...

    async def active_holds(
        self, presentation_id: UUID, *, page: int, size: int
    ) -> ReservationHoldPage:
        """The active reservations holding units of one presentation, soonest expiry first
        (ties by id), one page at a time."""
        ...

    async def low_stock_threshold(self) -> int:
        """The global low-stock threshold."""
        ...
