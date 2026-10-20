from datetime import datetime, timedelta

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert

from fragancia_api.modules.orders.infrastructure.tables import lookup_throttle
from fragancia_api.shared.infrastructure.database import Database

_row = lookup_throttle.c


class SqlLookupThrottle:
    """Fixed-window attempt counters in `orders.lookup_throttle`.

    A deliberate copy of identity's `SqlLoginThrottle.hit`, because modules may not import each
    other. `hit` is ONE statement (insert … on conflict do update … returning), so concurrent
    attempts for the same key are serialized by the row lock and each one sees its own count.
    It joins the active transaction.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def hit(self, key: str, now: datetime, window: timedelta) -> int:
        window_over = _row.window_started_at <= now - window
        statement = (
            insert(lookup_throttle)
            .values(key=key, attempts=1, window_started_at=now)
            .on_conflict_do_update(
                index_elements=[_row.key],
                set_={
                    "attempts": case((window_over, 1), else_=_row.attempts + 1),
                    "window_started_at": case((window_over, now), else_=_row.window_started_at),
                },
            )
            .returning(_row.attempts)
        )
        return int(await self._database.session.scalar(statement))  # RETURNING always yields
