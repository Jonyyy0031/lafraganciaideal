from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select

from fragancia_api.modules.inventory.contracts import StockMovement, StockMovementPage
from fragancia_api.modules.inventory.domain.stock import DEFAULT_LOW_STOCK_THRESHOLD
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SETTINGS_ROW_ID,
)
from fragancia_api.modules.inventory.infrastructure.tables import (
    settings,
    stock_items,
    stock_movements,
)
from fragancia_api.shared.infrastructure.database import Database


class SqlStockQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, presentation_id: UUID) -> tuple[int, int | None] | None:
        statement = select(stock_items.c.on_hand, stock_items.c.threshold_override).where(
            stock_items.c.presentation_id == presentation_id
        )
        async with self._database.reader() as session:
            row = (await session.execute(statement)).first()
        return (row[0], row[1]) if row is not None else None

    async def movements(self, presentation_id: UUID, *, page: int, size: int) -> StockMovementPage:
        where = stock_movements.c.presentation_id == presentation_id
        statement = (
            select(
                stock_movements.c.id,
                stock_movements.c.kind,
                stock_movements.c.quantity,
                stock_movements.c.delta,
                stock_movements.c.on_hand_after,
                stock_movements.c.reason,
                stock_movements.c.actor_id,
                stock_movements.c.created_at,
            )
            .where(where)
            .order_by(stock_movements.c.created_at.desc(), stock_movements.c.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
        async with self._database.reader() as session:
            total = (
                await session.scalar(select(func.count()).select_from(stock_movements).where(where))
                or 0
            )
            rows = (await session.execute(statement)).mappings()
            items = [StockMovement.model_validate(dict(row)) for row in rows]
        return StockMovementPage(items=items, total=total, page=page, size=size)

    async def on_hand_by_id(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int | None]]:
        if not ids:
            return {}
        statement = select(
            stock_items.c.presentation_id, stock_items.c.on_hand, stock_items.c.threshold_override
        ).where(stock_items.c.presentation_id.in_(list(ids)))
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).all()
        return {row[0]: (row[1], row[2]) for row in rows}

    async def low_stock_threshold(self) -> int:
        statement = select(settings.c.low_stock_threshold).where(settings.c.id == SETTINGS_ROW_ID)
        async with self._database.reader() as session:
            value = await session.scalar(statement)
        return value if value is not None else DEFAULT_LOW_STOCK_THRESHOLD
