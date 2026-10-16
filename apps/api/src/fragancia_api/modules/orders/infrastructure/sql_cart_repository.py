from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fragancia_api.modules.orders.domain.cart import Cart, CartLine
from fragancia_api.modules.orders.infrastructure.tables import cart_lines, carts
from fragancia_api.shared.infrastructure.database import Database


def _to_domain(row: Row[Any], lines: list[CartLine]) -> Cart:
    return Cart(
        id=row.id,
        token_digest=row.token_digest,
        lines=lines,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _load_lines(session: AsyncSession, cart_id: UUID) -> list[CartLine]:
    rows = (
        await session.execute(
            select(cart_lines)
            .where(cart_lines.c.cart_id == cart_id)
            .order_by(cart_lines.c.added_at, cart_lines.c.presentation_id)
        )
    ).all()
    return [
        CartLine(
            presentation_id=row.presentation_id,
            quantity=row.quantity,
            seen_price_cents=row.seen_price_cents,
            added_at=row.added_at,
        )
        for row in rows
    ]


async def _insert_lines(session: AsyncSession, cart: Cart) -> None:
    if cart.lines:
        await session.execute(
            insert(cart_lines),
            [
                {
                    "cart_id": cart.id,
                    "presentation_id": line.presentation_id,
                    "quantity": line.quantity,
                    "seen_price_cents": line.seen_price_cents,
                    "added_at": line.added_at,
                }
                for line in cart.lines
            ],
        )


class SqlCartRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_by_token_for_update(self, token_digest: str) -> Cart | None:
        session = self._database.session
        row = (
            await session.execute(
                select(carts).where(carts.c.token_digest == token_digest).with_for_update()
            )
        ).first()
        if row is None:
            return None
        return _to_domain(row, await _load_lines(session, row.id))

    async def add(self, cart: Cart) -> None:
        session = self._database.session
        await session.execute(
            insert(carts).values(
                id=cart.id,
                token_digest=cart.token_digest,
                created_at=cart.created_at,
                updated_at=cart.updated_at,
            )
        )
        await _insert_lines(session, cart)

    async def save(self, cart: Cart) -> None:
        session = self._database.session
        await session.execute(
            update(carts).where(carts.c.id == cart.id).values(updated_at=cart.updated_at)
        )
        await session.execute(delete(cart_lines).where(cart_lines.c.cart_id == cart.id))
        await _insert_lines(session, cart)

    async def delete_untouched_before(self, cutoff: datetime, limit: int) -> int:
        stale = (
            select(carts.c.id)
            .where(carts.c.updated_at < cutoff)
            .order_by(carts.c.updated_at)
            .limit(limit)
            .scalar_subquery()
        )
        result = await self._database.session.execute(
            delete(carts).where(carts.c.id.in_(stale)).returning(carts.c.id)
        )
        return len(result.all())


class SqlCartReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def by_token(self, token_digest: str) -> Cart | None:
        async with self._database.reader() as session:
            row = (
                await session.execute(select(carts).where(carts.c.token_digest == token_digest))
            ).first()
            if row is None:
                return None
            return _to_domain(row, await _load_lines(session, row.id))
