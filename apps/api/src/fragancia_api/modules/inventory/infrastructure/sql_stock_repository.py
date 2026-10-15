from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, insert, select
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


def _to_item(row: RowMapping) -> StockItem:
    override = row["threshold_override"]
    return StockItem(
        presentation_id=row["presentation_id"],
        on_hand=row["on_hand"],
        reserved=row["reserved"],
        threshold_override=Threshold(override) if override is not None else None,
        updated_at=row["updated_at"],
        low_alerted=row["low_alerted"],
    )


class SqlStockRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def _insert_empty(self, presentation_ids: list[UUID], now: datetime) -> None:
        # Empty items first, so two first changes on one presentation queue on its row lock
        # instead of both reading "nothing" and overwriting each other. Sorted, so concurrent
        # inserts of the same new rows take their index locks in one order.
        await self._database.session.execute(
            postgresql.insert(stock_items)
            .values(
                [
                    {
                        "presentation_id": presentation_id,
                        "on_hand": 0,
                        "reserved": 0,
                        "threshold_override": None,
                        "updated_at": now,
                    }
                    for presentation_id in presentation_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=[stock_items.c.presentation_id])
        )

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        await self._insert_empty([presentation_id], now)
        statement = (
            select(stock_items)
            .where(stock_items.c.presentation_id == presentation_id)
            .with_for_update()
        )
        row = (await self._database.session.execute(statement)).mappings().one()
        return _to_item(row)

    async def get_many_for_update(
        self, presentation_ids: Iterable[UUID], *, now: datetime
    ) -> dict[UUID, StockItem]:
        ids = sorted(set(presentation_ids))
        if not ids:
            return {}
        await self._insert_empty(ids, now)
        # Rows are locked in the ORDER BY order: the global lock order of stock items.
        statement = (
            select(stock_items)
            .where(stock_items.c.presentation_id.in_(ids))
            .order_by(stock_items.c.presentation_id)
            .with_for_update()
        )
        rows = (await self._database.session.execute(statement)).mappings().all()
        return {row["presentation_id"]: _to_item(row) for row in rows}

    async def list_all_for_update(self) -> list[StockItem]:
        statement = select(stock_items).order_by(stock_items.c.presentation_id).with_for_update()
        rows = (await self._database.session.execute(statement)).mappings().all()
        return [_to_item(row) for row in rows]

    async def save(self, item: StockItem) -> None:
        session = self._database.session
        values = {
            "on_hand": item.on_hand,
            "reserved": item.reserved,
            "threshold_override": (
                item.threshold_override.value if item.threshold_override is not None else None
            ),
            "low_alerted": item.low_alerted,
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
