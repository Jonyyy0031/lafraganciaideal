from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from fragancia_api.modules.identity.contracts import (
    AdminInvitation,
    AdminMe,
    AdminSession,
    AdminUser,
)
from fragancia_api.modules.identity.domain.user import ROLE_PERMISSIONS, Role
from fragancia_api.modules.identity.infrastructure.tables import invitations, sessions, users
from fragancia_api.shared.infrastructure.database import Database


class SqlAccountQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def me(self, user_id: UUID) -> AdminMe | None:
        statement = select(users.c.id, users.c.email, users.c.name, users.c.role).where(
            users.c.id == user_id
        )
        async with self._database.reader() as session:
            row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        return AdminMe(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            role=row["role"],
            permissions=sorted(ROLE_PERMISSIONS[Role(row["role"])]),
        )

    async def sessions(
        self, user_id: UUID, *, current_session_id: UUID, now: datetime, idle: timedelta
    ) -> list[AdminSession]:
        statement = (
            select(
                sessions.c.id,
                sessions.c.created_at,
                sessions.c.last_seen_at,
                sessions.c.expires_at,
                sessions.c.user_agent,
                sessions.c.ip,
            )
            .where(
                sessions.c.user_id == user_id,
                sessions.c.revoked_at.is_(None),
                sessions.c.expires_at > now,
                sessions.c.last_seen_at > now - idle,
            )
            .order_by(sessions.c.last_seen_at.desc(), sessions.c.id.desc())
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [
            AdminSession.model_validate({**row, "current": row["id"] == current_session_id})
            for row in rows
        ]

    async def users(self) -> list[AdminUser]:
        statement = select(
            users.c.id,
            users.c.email,
            users.c.name,
            users.c.role,
            users.c.is_active,
            users.c.created_at,
        ).order_by(users.c.created_at, users.c.id)
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [AdminUser.model_validate(dict(row)) for row in rows]

    async def pending_invitations(self, now: datetime) -> list[AdminInvitation]:
        statement = (
            select(
                invitations.c.id,
                invitations.c.email,
                invitations.c.name,
                invitations.c.created_at,
                invitations.c.expires_at,
            )
            .where(
                invitations.c.accepted_at.is_(None),
                invitations.c.revoked_at.is_(None),
                invitations.c.expires_at > now,
            )
            .order_by(invitations.c.created_at.desc(), invitations.c.id.desc())
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [AdminInvitation.model_validate(dict(row)) for row in rows]
