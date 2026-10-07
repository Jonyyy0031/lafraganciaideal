from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, insert, select, update

from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.infrastructure.tables import password_resets
from fragancia_api.shared.infrastructure.database import Database


def _to_row(reset: PasswordReset) -> dict[str, Any]:
    return {
        "id": reset.id,
        "user_id": reset.user_id,
        "created_at": reset.created_at,
        "expires_at": reset.expires_at,
        "token_hash": reset.token_hash,
        "used_at": reset.used_at,
        "cancelled_at": reset.cancelled_at,
    }


def _to_reset(row: RowMapping) -> PasswordReset:
    return PasswordReset(
        id=row["id"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        token_hash=row["token_hash"],
        used_at=row["used_at"],
        cancelled_at=row["cancelled_at"],
    )


class SqlPasswordResetRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def add(self, reset: PasswordReset) -> None:
        await self._database.session.execute(insert(password_resets).values(_to_row(reset)))

    async def get(self, reset_id: UUID, *, for_update: bool = False) -> PasswordReset | None:
        statement = select(password_resets).where(password_resets.c.id == reset_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(statement)

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> PasswordReset | None:
        statement = select(password_resets).where(password_resets.c.token_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(statement)

    async def save(self, reset: PasswordReset) -> None:
        await self._database.session.execute(
            update(password_resets)
            .where(password_resets.c.id == reset.id)
            .values(
                token_hash=reset.token_hash,
                used_at=reset.used_at,
                cancelled_at=reset.cancelled_at,
            )
        )

    async def cancel_open_for_user(self, user_id: UUID, *, now: datetime) -> None:
        await self._database.session.execute(
            update(password_resets)
            .where(
                password_resets.c.user_id == user_id,
                password_resets.c.used_at.is_(None),
                password_resets.c.cancelled_at.is_(None),
            )
            .values(cancelled_at=now)
        )

    async def _one(self, statement: Any) -> PasswordReset | None:
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_reset(row) if row is not None else None
