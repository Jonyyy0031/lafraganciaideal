from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, update
from sqlalchemy.dialects.postgresql import insert

from fragancia_api.modules.identity.infrastructure.tables import login_throttle
from fragancia_api.shared.infrastructure.database import Database

_row = login_throttle.c


class SqlLoginThrottle:
    """Fixed-window attempt counters in `identity.login_throttle`.

    `hit` is ONE statement (insert … on conflict do update … returning), so concurrent attempts
    for the same key are serialized by the row lock and each one sees its own count.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def hit(self, key: str, *, now: datetime, window: timedelta) -> int:
        window_over = _row.window_started_at <= now - window
        statement = (
            insert(login_throttle)
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

    async def clear(self, key: str) -> None:
        await self._database.session.execute(delete(login_throttle).where(_row.key == key))

    async def give_back(self, key: str) -> None:
        await self._database.session.execute(
            update(login_throttle)
            .where(_row.key == key)
            .values(attempts=func.greatest(_row.attempts - 1, 0))
        )
