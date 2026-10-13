from typing import Protocol
from uuid import UUID

from fragancia_api.modules.inventory.domain.stock import InventorySettings, StockItem


class StockRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see StockQueries."""

    async def get_for_update(self, presentation_id: UUID) -> StockItem | None:
        """The stock item with its row locked until the transaction ends. The SQL adapter
        first inserts an empty item (`ON CONFLICT DO NOTHING`) so that two concurrent first
        movements on one presentation serialize; the fakes return None when the presentation
        was never counted. Callers verify the presentation exists before calling."""
        ...

    async def save(self, item: StockItem) -> None:
        """Upsert the item and insert its `new_movements` (then forget them)."""
        ...


class SettingsRepository(Protocol):
    async def get(self) -> InventorySettings:
        """The global settings (the default when nothing is stored)."""
        ...

    async def save(self, settings: InventorySettings) -> None: ...
