"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.contracts import AdminMe, AdminSession
from fragancia_api.modules.identity.domain.errors import EmailTaken
from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.modules.identity.domain.user import Email, User
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryUsers:
    """The user repository over a dict."""

    def __init__(self, *users: User) -> None:
        self.by_id: dict[UUID, User] = {user.id: user for user in users}

    async def get(self, user_id: UUID) -> User | None:
        return self.by_id.get(user_id)

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


class InMemoryAccountQueries:
    """The account queries over the in-memory repositories."""

    def __init__(self, users: InMemoryUsers, sessions: InMemorySessions) -> None:
        self._users = users
        self._sessions = sessions

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

    def hash(self, password: str) -> str:
        return f"plain:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        self.verified_hashes.append(password_hash)
        return password_hash == f"plain:{password}"


class SequentialSessionTokens:
    """Predictable tokens (`token-1`, `token-2`, …) and digests (`digest:<token>`)."""

    def __init__(self) -> None:
        self._issued = 0

    def new(self) -> str:
        self._issued += 1
        return f"token-{self._issued}"

    def digest(self, token: str) -> str:
        return f"digest:{token}"
