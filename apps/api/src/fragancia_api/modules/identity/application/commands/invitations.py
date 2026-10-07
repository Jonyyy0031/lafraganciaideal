from uuid import UUID

from fragancia_api.modules.identity.application.policy import AccountLinks
from fragancia_api.modules.identity.application.ports import PasswordHasher, SessionTokens
from fragancia_api.modules.identity.domain.errors import (
    EmailTaken,
    InvitationNotFound,
    LinkInvalid,
)
from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.repositories import (
    InvitationRepository,
    UserRepository,
)
from fragancia_api.modules.identity.domain.user import (
    DisplayName,
    Email,
    PlainPassword,
    Role,
    User,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class InviteUser:
    """Command: invite someone by email to become staff. Inviting the same email again revokes
    the previous open invitation. The email itself is sent by the worker (outbox)."""

    def __init__(
        self,
        *,
        users: UserRepository,
        invitations: InvitationRepository,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
        links: AccountLinks,
    ) -> None:
        self._users = users
        self._invitations = invitations
        self._transactions = transactions
        self._events = events
        self._clock = clock
        self._links = links

    async def execute(self, invited_by: UUID, email: str, name: str) -> Result[UUID, DomainError]:
        match Email.create(email):
            case Err(invalid_email):
                return Err(invalid_email)
            case Ok(valid_email):
                pass
        match DisplayName.create(name):
            case Err(invalid_name):
                return Err(invalid_name)
            case Ok(display_name):
                pass

        async def work() -> Result[UUID, DomainError]:
            now = self._clock.now()
            # Revoke first: the UPDATE waits for an acceptance in flight on the same invitation,
            # so the user check below sees the account it creates. An `Err` rolls the revoke back.
            await self._invitations.revoke_open_for_email(valid_email, now=now)
            if await self._users.get_by_email(valid_email) is not None:
                return Err(EmailTaken())
            invitation = Invitation.issue(
                valid_email, display_name, invited_by, now=now, ttl=self._links.invitation_ttl
            )
            await self._invitations.add(invitation)
            await self._events.publish(invitation.pull_events())
            return Ok(invitation.id)

        return await self._transactions.run(work)


class RevokeInvitation:
    """Command: cancel a pending invitation."""

    def __init__(
        self,
        *,
        invitations: InvitationRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._invitations = invitations
        self._transactions = transactions
        self._clock = clock

    async def execute(self, invitation_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            now = self._clock.now()
            invitation = await self._invitations.get(invitation_id, for_update=True)
            if invitation is None or not invitation.is_pending(now):
                return Err(InvitationNotFound())
            invitation.revoke(now)
            await self._invitations.save(invitation)
            return Ok(None)

        return await self._transactions.run(work)


class AcceptInvitation:
    """Command: turn an emailed invitation link into a staff account with the chosen password."""

    def __init__(
        self,
        *,
        users: UserRepository,
        invitations: InvitationRepository,
        hasher: PasswordHasher,
        tokens: SessionTokens,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._users = users
        self._invitations = invitations
        self._hasher = hasher
        self._tokens = tokens
        self._transactions = transactions
        self._clock = clock

    async def execute(self, token: str, password: str) -> Result[UUID, DomainError]:
        match PlainPassword.create(password):
            case Err(weak):
                return Err(weak)
            case Ok(plain):
                pass
        digest = self._tokens.digest(token)

        async def check() -> Result[None, DomainError]:
            invitation = await self._invitations.get_by_token_hash(digest)
            if invitation is None or not invitation.is_pending(self._clock.now()):
                return Err(LinkInvalid())
            return Ok(None)

        match await self._transactions.run(check):
            case Err(error):
                return Err(error)
            case Ok(_):
                pass

        password_hash = await self._hasher.hash(plain.value)  # slow: outside any transaction

        async def work() -> Result[UUID, DomainError]:
            now = self._clock.now()
            invitation = await self._invitations.get_by_token_hash(digest, for_update=True)
            if invitation is None or not invitation.is_pending(now):
                return Err(LinkInvalid())
            user = User.create(
                invitation.email, invitation.name, Role.STAFF, password_hash, now=now
            )
            match await self._users.add(user):
                case Err(conflict):
                    return Err(conflict)
            invitation.accept(now)
            await self._invitations.save(invitation)
            return Ok(user.id)

        return await self._transactions.run(work)
