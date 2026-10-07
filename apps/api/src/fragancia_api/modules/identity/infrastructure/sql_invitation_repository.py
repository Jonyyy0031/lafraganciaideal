from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, insert, select, update

from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.user import DisplayName, Email
from fragancia_api.modules.identity.infrastructure.tables import invitations
from fragancia_api.shared.infrastructure.database import Database


def _to_row(invitation: Invitation) -> dict[str, Any]:
    return {
        "id": invitation.id,
        "email": invitation.email.value,
        "name": invitation.name.value,
        "invited_by": invitation.invited_by,
        "created_at": invitation.created_at,
        "expires_at": invitation.expires_at,
        "token_hash": invitation.token_hash,
        "accepted_at": invitation.accepted_at,
        "revoked_at": invitation.revoked_at,
    }


def _to_invitation(row: RowMapping) -> Invitation:
    return Invitation(
        id=row["id"],
        email=Email(row["email"]),
        name=DisplayName(row["name"]),
        invited_by=row["invited_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        token_hash=row["token_hash"],
        accepted_at=row["accepted_at"],
        revoked_at=row["revoked_at"],
    )


class SqlInvitationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def add(self, invitation: Invitation) -> None:
        await self._database.session.execute(insert(invitations).values(_to_row(invitation)))

    async def get(self, invitation_id: UUID, *, for_update: bool = False) -> Invitation | None:
        statement = select(invitations).where(invitations.c.id == invitation_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(statement)

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> Invitation | None:
        statement = select(invitations).where(invitations.c.token_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(statement)

    async def save(self, invitation: Invitation) -> None:
        await self._database.session.execute(
            update(invitations)
            .where(invitations.c.id == invitation.id)
            .values(
                token_hash=invitation.token_hash,
                accepted_at=invitation.accepted_at,
                revoked_at=invitation.revoked_at,
            )
        )

    async def revoke_open_for_email(self, email: Email, *, now: datetime) -> None:
        await self._database.session.execute(
            update(invitations)
            .where(
                invitations.c.email == email.value,
                invitations.c.accepted_at.is_(None),
                invitations.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    async def _one(self, statement: Any) -> Invitation | None:
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_invitation(row) if row is not None else None
