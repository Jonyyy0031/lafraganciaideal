from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, update

from fragancia_api.modules.catalog.infrastructure.tables import presentations
from fragancia_api.shared.infrastructure.database import Database


class SqlStockCopy:
    """Writes the stock figures copied onto `catalog.presentations`; joins the active
    transaction (the relay's)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def apply(
        self, presentation_id: UUID, available: int, threshold: int, as_of: datetime
    ) -> None:
        await self._database.session.execute(
            update(presentations)
            .where(
                presentations.c.id == presentation_id,
                or_(presentations.c.stock_as_of.is_(None), presentations.c.stock_as_of < as_of),
            )
            .values(stock_available=available, stock_threshold=threshold, stock_as_of=as_of)
        )
