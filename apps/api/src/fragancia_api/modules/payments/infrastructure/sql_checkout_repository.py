from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from fragancia_api.modules.payments.domain.checkout import Checkout
from fragancia_api.modules.payments.infrastructure.tables import checkouts
from fragancia_api.shared.infrastructure.database import Database


class SqlCheckoutRepository:
    """Checkouts in `payments.checkouts`. Joins the active transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_for_order(self, order_id: UUID) -> Checkout | None:
        row = (
            await self._database.session.execute(
                select(checkouts).where(checkouts.c.order_id == order_id)
            )
        ).first()
        if row is None:
            return None
        return Checkout(
            order_id=row.order_id,
            preference_id=row.preference_id,
            redirect_url=row.redirect_url,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    async def upsert(self, checkout: Checkout) -> None:
        values = {
            "preference_id": checkout.preference_id,
            "redirect_url": checkout.redirect_url,
            "expires_at": checkout.expires_at,
            "created_at": checkout.created_at,
        }
        await self._database.session.execute(
            insert(checkouts)
            .values(order_id=checkout.order_id, **values)
            .on_conflict_do_update(index_elements=[checkouts.c.order_id], set_=values)
        )

    async def close(self, order_id: UUID, at: datetime) -> None:
        await self._database.session.execute(
            update(checkouts).where(checkouts.c.order_id == order_id).values(expires_at=at)
        )
