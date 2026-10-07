"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.contracts import (
    AdminInvitation,
    AdminMe,
    AdminSession,
    AdminUser,
)
from fragancia_api.modules.identity.domain.errors import EmailTaken
from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.modules.identity.domain.user import Email, User
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryUsers:
    """The user repository over a dict."""

    def __init__(self, *users: User) -> None:
        self.by_id: dict[UUID, User] = {user.id: user for user in users}

    async def get(self, user_id: UUID) -> User | None:
        return self.by_id.get(user_id)

    async def get_for_update(self, user_id: UUID) -> User | None:
        return await self.get(user_id)

    async def get_by_email(self, email: Email) -> User | None:
        return next((u for u in self.by_id.values() if u.email == email), None)

    async def add(self, user: User) -> Result[None, EmailTaken]:
        if await self.get_by_email(user.email) is not None:
            return Err(EmailTaken())
        self.by_id[user.id] = user
        return Ok(None)

    async def save(self, user: User) -> None:
        self.by_id[user.id] = user


class InMemorySessions:
    """The session repository over a dict."""

    def __init__(self, *sessions: Session) -> None:
        self.by_id: dict[UUID, Session] = {session.id: session for session in sessions}

    async def add(self, session: Session) -> None:
        self.by_id[session.id] = session

    async def get(self, session_id: UUID) -> Session | None:
        return self.by_id.get(session_id)

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        return next((s for s in self.by_id.values() if s.token_hash == token_hash), None)

    async def save(self, session: Session) -> None:
        self.by_id[session.id] = session

    async def revoke_all_for_user(
        self, user_id: UUID, *, except_id: UUID | None, now: datetime
    ) -> None:
        for session in self.by_id.values():
            if session.user_id == user_id and session.id != except_id:
                session.revoke(now)


class InMemoryLoginThrottle:
    """Fixed-window counters: `counts[key] = (attempts, window_started_at)`."""

    def __init__(self) -> None:
        self.counts: dict[str, tuple[int, datetime]] = {}

    async def hit(self, key: str, *, now: datetime, window: timedelta) -> int:
        current = self.counts.get(key)
        if current is None or current[1] <= now - window:
            self.counts[key] = (1, now)
        else:
            self.counts[key] = (current[0] + 1, current[1])
        return self.counts[key][0]

    async def clear(self, key: str) -> None:
        self.counts.pop(key, None)

    async def give_back(self, key: str) -> None:
        if key in self.counts:
            attempts, started = self.counts[key]
            self.counts[key] = (max(attempts - 1, 0), started)


class InMemoryInvitations:
    """The invitation repository over a dict."""

    def __init__(self, *invitations: Invitation) -> None:
        self.by_id: dict[UUID, Invitation] = {i.id: i for i in invitations}

    async def add(self, invitation: Invitation) -> None:
        self.by_id[invitation.id] = invitation

    async def get(self, invitation_id: UUID, *, for_update: bool = False) -> Invitation | None:
        return self.by_id.get(invitation_id)

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> Invitation | None:
        return next((i for i in self.by_id.values() if i.token_hash == token_hash), None)

    async def save(self, invitation: Invitation) -> None:
        self.by_id[invitation.id] = invitation

    async def revoke_open_for_email(self, email: Email, *, now: datetime) -> None:
        for invitation in self.by_id.values():
            if invitation.email == email:
                invitation.revoke(now)


class InMemoryPasswordResets:
    """The password-reset repository over a dict."""

    def __init__(self, *resets: PasswordReset) -> None:
        self.by_id: dict[UUID, PasswordReset] = {r.id: r for r in resets}

    async def add(self, reset: PasswordReset) -> None:
        self.by_id[reset.id] = reset

    async def get(self, reset_id: UUID, *, for_update: bool = False) -> PasswordReset | None:
        return self.by_id.get(reset_id)

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> PasswordReset | None:
        return next((r for r in self.by_id.values() if r.token_hash == token_hash), None)

    async def save(self, reset: PasswordReset) -> None:
        self.by_id[reset.id] = reset

    async def cancel_open_for_user(self, user_id: UUID, *, now: datetime) -> None:
        for reset in self.by_id.values():
            if reset.user_id == user_id and reset.used_at is None and reset.cancelled_at is None:
                reset.cancelled_at = now


class InMemoryAccountQueries:
    """The account queries over the in-memory repositories."""

    def __init__(
        self,
        users: InMemoryUsers,
        sessions: InMemorySessions,
        invitations: InMemoryInvitations | None = None,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._invitations = invitations or InMemoryInvitations()

    async def users(self) -> list[AdminUser]:
        ordered = sorted(self._users.by_id.values(), key=lambda u: (u.created_at, u.id))
        return [
            AdminUser(
                id=u.id,
                email=u.email.value,
                name=u.name.value,
                role=u.role.value,
                is_active=u.is_active,
                created_at=u.created_at,
            )
            for u in ordered
        ]

    async def pending_invitations(self, now: datetime) -> list[AdminInvitation]:
        pending = [i for i in self._invitations.by_id.values() if i.is_pending(now)]
        pending.sort(key=lambda i: (i.created_at, i.id), reverse=True)
        return [
            AdminInvitation(
                id=i.id,
                email=i.email.value,
                name=i.name.value,
                created_at=i.created_at,
                expires_at=i.expires_at,
            )
            for i in pending
        ]

    async def me(self, user_id: UUID) -> AdminMe | None:
        user = self._users.by_id.get(user_id)
        if user is None:
            return None
        return AdminMe(
            id=user.id,
            email=user.email.value,
            name=user.name.value,
            role=user.role.value,
            permissions=sorted(user.permissions),
        )

    async def sessions(
        self, user_id: UUID, *, current_session_id: UUID, now: datetime, idle: timedelta
    ) -> list[AdminSession]:
        valid = [
            s
            for s in self._sessions.by_id.values()
            if s.user_id == user_id and s.is_valid(now, idle)
        ]
        valid.sort(key=lambda s: (s.last_seen_at, s.id), reverse=True)
        return [
            AdminSession(
                id=s.id,
                created_at=s.created_at,
                last_seen_at=s.last_seen_at,
                expires_at=s.expires_at,
                user_agent=s.user_agent,
                ip=s.ip,
                current=s.id == current_session_id,
            )
            for s in valid
        ]


class PlainTextPasswordHasher:
    """UNIT TESTS ONLY: the "hash" is the password with a prefix. Records what it verified."""

    dummy_hash = "plain:<dummy>"

    def __init__(self) -> None:
        self.verified_hashes: list[str] = []

    @staticmethod
    def encode(password: str) -> str:
        """The "hash" of a password, synchronously (for test setup and assertions)."""
        return f"plain:{password}"

    async def hash(self, password: str) -> str:
        return self.encode(password)

    async def verify(self, password_hash: str, password: str) -> bool:
        self.verified_hashes.append(password_hash)
        return password_hash == self.encode(password)


class SequentialSessionTokens:
    """Predictable tokens (`token-1`, `token-2`, …) and digests (`digest:<token>`)."""

    def __init__(self) -> None:
        self._issued = 0

    def new(self) -> str:
        self._issued += 1
        return f"token-{self._issued}"

    def digest(self, token: str) -> str:
        return f"digest:{token}"
