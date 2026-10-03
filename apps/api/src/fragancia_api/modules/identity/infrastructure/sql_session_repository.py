from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, insert, select, update

from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.modules.identity.infrastructure.tables import sessions
from fragancia_api.shared.infrastructure.database import Database


def _to_row(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "user_id": session.user_id,
        "token_hash": session.token_hash,
        "created_at": session.created_at,
        "last_seen_at": session.last_seen_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "user_agent": session.user_agent,
        "ip": session.ip,
    }


def _to_session(row: RowMapping) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        user_agent=row["user_agent"],
        ip=row["ip"],
    )


class SqlSessionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def add(self, session: Session) -> None:
        await self._database.session.execute(insert(sessions).values(_to_row(session)))

    async def get(self, session_id: UUID) -> Session | None:
        return await self._one(select(sessions).where(sessions.c.id == session_id))

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        return await self._one(select(sessions).where(sessions.c.token_hash == token_hash))

    async def save(self, session: Session) -> None:
        await self._database.session.execute(
            update(sessions)
            .where(sessions.c.id == session.id)
            .values(last_seen_at=session.last_seen_at, revoked_at=session.revoked_at)
        )

    async def revoke_all_for_user(
        self, user_id: UUID, *, except_id: UUID | None, now: datetime
    ) -> None:
        statement = (
            update(sessions)
            .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        if except_id is not None:
            statement = statement.where(sessions.c.id != except_id)
        await self._database.session.execute(statement)

    async def _one(self, statement: Any) -> Session | None:
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_session(row) if row is not None else None
