from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from fragancia_api.modules.inventory.domain.stock import InventorySettings, Threshold
from fragancia_api.modules.inventory.infrastructure.tables import settings
from fragancia_api.shared.infrastructure.database import Database

SETTINGS_ROW_ID = 1


class SqlSettingsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self) -> InventorySettings:
        statement = select(settings.c.low_stock_threshold).where(settings.c.id == SETTINGS_ROW_ID)
        value = await self._database.session.scalar(statement)
        if value is None:
            return InventorySettings()
        return InventorySettings(low_stock_threshold=Threshold(value))

    async def save(self, new: InventorySettings) -> None:
        values = {"low_stock_threshold": new.low_stock_threshold.value}
        await self._database.session.execute(
            postgresql.insert(settings)
            .values(id=SETTINGS_ROW_ID, **values)
            .on_conflict_do_update(index_elements=[settings.c.id], set_=values)
        )
