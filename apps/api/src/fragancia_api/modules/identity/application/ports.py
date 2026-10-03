from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.identity.contracts import AdminMe, AdminSession


class PasswordHasher(Protocol):
    """Slow, salted password hashing. Async so that the CPU work runs off the event loop."""

    dummy_hash: str
    """A hash of a random password, verified when the user does not exist so that timing does
    not reveal which accounts exist."""

    async def hash(self, password: str) -> str: ...

    async def verify(self, password_hash: str, password: str) -> bool:
        """False on a mismatch or an unreadable hash; never raises for those."""
        ...


class SessionTokens(Protocol):
    """Opaque session tokens: only their digest is stored."""

    def new(self) -> str:
        """A new URL-safe random token."""
        ...

    def digest(self, token: str) -> str:
        """Hex SHA-256 of the token."""
        ...


class LoginThrottle(Protocol):
    """Login attempt counters per key (fixed windows). Joins the active transaction."""

    async def hit(self, key: str, *, now: datetime, window: timedelta) -> int:
        """Atomically count one attempt in the key's window and return the count. A new window
        starts when the previous one is over."""
        ...

    async def clear(self, key: str) -> None: ...

    async def give_back(self, key: str) -> None:
        """Undo one attempt; never below 0."""
        ...


class AccountQueries(Protocol):
    """Read side: returns response models directly, no aggregates involved."""

    async def me(self, user_id: UUID) -> AdminMe | None: ...

    async def sessions(
        self, user_id: UUID, *, current_session_id: UUID, now: datetime, idle: timedelta
    ) -> list[AdminSession]:
        """The user's valid sessions, most recently seen first."""
        ...
