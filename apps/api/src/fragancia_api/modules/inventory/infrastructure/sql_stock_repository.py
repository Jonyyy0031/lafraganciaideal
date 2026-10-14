from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects import postgresql

from fragancia_api.modules.inventory.domain.stock import Movement, StockItem, Threshold
from fragancia_api.modules.inventory.infrastructure.tables import stock_items, stock_movements
from fragancia_api.shared.infrastructure.database import Database


def _movement_row(movement: Movement) -> dict[str, Any]:
    return {
        "id": movement.id,
        "presentation_id": movement.presentation_id,
        "kind": movement.kind.value,
        "quantity": movement.quantity,
        "delta": movement.delta,
        "on_hand_after": movement.on_hand_after,
        "reason": movement.reason.value,
        "actor_id": movement.actor_id,
        "created_at": movement.created_at,
    }


class SqlStockRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        session = self._database.session
        # An empty item first, so two first movements on one presentation queue on its row lock
        # instead of both reading "nothing" and overwriting each other.
        await session.execute(
            postgresql.insert(stock_items)
            .values(
                presentation_id=presentation_id,
                on_hand=0,
                threshold_override=None,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[stock_items.c.presentation_id])
        )
        statement = (
            select(stock_items)
            .where(stock_items.c.presentation_id == presentation_id)
            .with_for_update()
        )
        row = (await session.execute(statement)).mappings().one()
        override = row["threshold_override"]
        return StockItem(
            presentation_id=row["presentation_id"],
            on_hand=row["on_hand"],
            threshold_override=Threshold(override) if override is not None else None,
            updated_at=row["updated_at"],
        )

    async def list_all_for_update(self) -> list[StockItem]:
        statement = select(stock_items).order_by(stock_items.c.presentation_id).with_for_update()
        rows = (await self._database.session.execute(statement)).mappings().all()
        return [
            StockItem(
                presentation_id=row["presentation_id"],
                on_hand=row["on_hand"],
                threshold_override=(
                    Threshold(row["threshold_override"])
                    if row["threshold_override"] is not None
                    else None
                ),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def save(self, item: StockItem) -> None:
        session = self._database.session
        values = {
            "on_hand": item.on_hand,
            "threshold_override": (
                item.threshold_override.value if item.threshold_override is not None else None
            ),
            "updated_at": item.updated_at,
        }
        await session.execute(
            postgresql.insert(stock_items)
            .values(presentation_id=item.presentation_id, **values)
            .on_conflict_do_update(index_elements=[stock_items.c.presentation_id], set_=values)
        )
        if item.new_movements:
            await session.execute(
                insert(stock_movements), [_movement_row(m) for m in item.new_movements]
            )
            item.new_movements.clear()
