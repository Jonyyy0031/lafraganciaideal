"""An invitation for someone to become back-office staff.

The invitee's role is always `Role.STAFF` (decision 11), so there is no role field. The link's
token is minted when the email is sent; the invitation stores only its digest.
"""

from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.events import InvitationIssued
from fragancia_api.modules.identity.domain.user import DisplayName, Email
from fragancia_api.shared.kernel import AggregateRoot, new_id


class Invitation(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        email: Email,
        name: DisplayName,
        invited_by: UUID,
        created_at: datetime,
        expires_at: datetime,
        token_hash: str | None,
        accepted_at: datetime | None,
        revoked_at: datetime | None,
    ) -> None:
        super().__init__()
        self.id = id
        self.email = email
        self.name = name
        self.invited_by = invited_by
        self.created_at = created_at
        self.expires_at = expires_at
        self.token_hash = token_hash
        self.accepted_at = accepted_at
        self.revoked_at = revoked_at

    @classmethod
    def issue(
        cls,
        email: Email,
        name: DisplayName,
        invited_by: UUID,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> Invitation:
        invitation = cls(
            id=new_id(),
            email=email,
            name=name,
            invited_by=invited_by,
            created_at=now,
            expires_at=now + ttl,
            token_hash=None,
            accepted_at=None,
            revoked_at=None,
        )
        invitation.record(InvitationIssued(invitation_id=invitation.id))
        return invitation

    def is_pending(self, now: datetime) -> bool:
        return self.accepted_at is None and self.revoked_at is None and now < self.expires_at

    def attach_token(self, token_hash: str) -> None:
        self.token_hash = token_hash

    def accept(self, now: datetime) -> None:
        if self.accepted_at is None and self.revoked_at is None:
            self.accepted_at = now

    def revoke(self, now: datetime) -> None:
        if self.accepted_at is None and self.revoked_at is None:
            self.revoked_at = now
