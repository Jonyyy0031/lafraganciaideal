"""Identity domain events. They carry only ids: the emailed token is minted by the subscriber."""

from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from fragancia_api.shared.kernel import DomainEvent


@dataclass(frozen=True, kw_only=True)
class InvitationIssued(DomainEvent):
    name: ClassVar[str] = "identity.invitation.issued"
    invitation_id: UUID


@dataclass(frozen=True, kw_only=True)
class PasswordResetRequested(DomainEvent):
    name: ClassVar[str] = "identity.password_reset.requested"
    reset_id: UUID
