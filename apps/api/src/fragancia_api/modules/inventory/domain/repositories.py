from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.inventory.domain.stock import InventorySettings, StockItem


class StockRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see StockQueries."""

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        """The stock item with its row locked until the transaction ends. The SQL adapter
        first inserts an empty item stamped `now` (`ON CONFLICT DO NOTHING`) so that two
        concurrent first movements on one presentation serialize; the fakes return None when the
        presentation was never counted. Callers verify the presentation exists before calling."""
        ...

    async def list_all_for_update(self) -> list[StockItem]:
        """Every stock item with its row locked until the transaction ends, ordered by
        `presentation_id` (the lock order of a global settings change)."""
        ...

    async def save(self, item: StockItem) -> None:
        """Upsert the item and insert its `new_movements` (then forget them)."""
        ...


class SettingsRepository(Protocol):
    async def get(self) -> InventorySettings:
        """The global settings (the default when nothing is stored)."""
        ...

    async def get_for_update(self) -> InventorySettings:
        """The global settings with their row locked `FOR UPDATE` until the transaction ends (a
        settings change). The SQL adapter first inserts the default row when none is stored, so
        there is always a row to lock."""
        ...

    async def get_for_share(self) -> InventorySettings:
        """The global settings with their row locked `FOR SHARE` until the transaction ends (a
        movement or threshold change: they wait for a settings change in progress, and a
        settings change waits for them)."""
        ...

    async def save(self, settings: InventorySettings) -> None: ...
