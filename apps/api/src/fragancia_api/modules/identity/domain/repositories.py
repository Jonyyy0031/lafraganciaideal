from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.identity.domain.errors import EmailTaken
from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.modules.identity.domain.user import Email, User
from fragancia_api.shared.kernel import Result


class UserRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see AccountQueries."""

    async def get(self, user_id: UUID) -> User | None: ...

    async def get_by_email(self, email: Email) -> User | None: ...

    async def add(self, user: User) -> Result[None, EmailTaken]:
        """Err when another user already has the email (also under concurrent inserts)."""
        ...

    async def save(self, user: User) -> None:
        """Persist `password_hash` and `password_changed_at`."""
        ...


class SessionRepository(Protocol):
    """Write side. Joins the active transaction."""

    async def add(self, session: Session) -> None: ...

    async def get(self, session_id: UUID) -> Session | None: ...

    async def get_by_token_hash(self, token_hash: str) -> Session | None: ...

    async def save(self, session: Session) -> None:
        """Persist `last_seen_at` and `revoked_at`."""
        ...

    async def revoke_all_for_user(
        self, user_id: UUID, *, except_id: UUID | None, now: datetime
    ) -> None:
        """Set `revoked_at` on every non-revoked session of the user except `except_id`."""
        ...
