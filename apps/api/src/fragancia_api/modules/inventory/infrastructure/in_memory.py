"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.inventory.application.ports import StockedPresentation
from fragancia_api.modules.inventory.contracts import StockMovement, StockMovementPage
from fragancia_api.modules.inventory.domain.stock import (
    InventorySettings,
    Movement,
    StockItem,
)


class InMemoryStock:
    """Both the repository and the queries over one store, plus the settings."""

    def __init__(self, settings: InventorySettings | None = None) -> None:
        self.items: dict[UUID, StockItem] = {}
        self.movements_log: list[Movement] = []
        self.settings = settings if settings is not None else InventorySettings()

    # StockRepository

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        item = self.items.get(presentation_id)
        return deepcopy(item) if item is not None else None

    async def save(self, item: StockItem) -> None:
        self.movements_log.extend(item.new_movements)
        item.new_movements.clear()
        self.items[item.presentation_id] = deepcopy(item)

    # StockQueries

    async def get(self, presentation_id: UUID) -> tuple[int, int | None] | None:
        item = self.items.get(presentation_id)
        if item is None:
            return None
        override = item.threshold_override
        return (item.on_hand, override.value if override is not None else None)

    async def movements(self, presentation_id: UUID, *, page: int, size: int) -> StockMovementPage:
        own = sorted(
            (m for m in self.movements_log if m.presentation_id == presentation_id),
            key=lambda m: (m.created_at, m.id),
            reverse=True,
        )
        start = (page - 1) * size
        items = [
            StockMovement(
                id=m.id,
                kind=m.kind.value,
                quantity=m.quantity,
                delta=m.delta,
                on_hand_after=m.on_hand_after,
                reason=m.reason.value,
                actor_id=m.actor_id,
                created_at=m.created_at,
            )
            for m in own[start : start + size]
        ]
        return StockMovementPage(items=items, total=len(own), page=page, size=size)

    async def on_hand_by_id(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int | None]]:
        found: dict[UUID, tuple[int, int | None]] = {}
        for presentation_id in ids:
            counted = await self.get(presentation_id)
            if counted is not None:
                found[presentation_id] = counted
        return found

    async def low_stock_threshold(self) -> int:
        return self.settings.low_stock_threshold.value


class InMemorySettings:
    """The `SettingsRepository` over the settings of an `InMemoryStock`."""

    def __init__(self, stock: InMemoryStock) -> None:
        self._stock = stock

    async def get(self) -> InventorySettings:
        return self._stock.settings

    async def save(self, settings: InventorySettings) -> None:
        self._stock.settings = settings


class InMemoryPresentationCatalog:
    """The `PresentationCatalog` over a fixed list, in display order."""

    def __init__(self, *presentations: StockedPresentation) -> None:
        self.presentations = list(presentations)

    async def describe(self, ids: Sequence[UUID]) -> dict[UUID, StockedPresentation]:
        wanted = set(ids)
        return {p.presentation_id: p for p in self.presentations if p.presentation_id in wanted}

    async def describe_for_update(self, presentation_id: UUID) -> StockedPresentation | None:
        return (await self.describe([presentation_id])).get(presentation_id)

    async def in_stock(self) -> list[StockedPresentation]:
        return [p for p in self.presentations if p.stocked]
