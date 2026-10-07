from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.modules.identity.domain.errors import EmailTaken
from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.password_reset import PasswordReset
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

    async def get_for_update(self, user_id: UUID) -> User | None:
        """Lock the row until the transaction ends."""
        ...

    async def save(self, user: User) -> None:
        """Persist `password_hash`, `password_changed_at` and `is_active`."""
        ...


class InvitationRepository(Protocol):
    """Write side. Joins the active transaction."""

    async def add(self, invitation: Invitation) -> None: ...

    async def get(self, invitation_id: UUID, *, for_update: bool = False) -> Invitation | None:
        """With `for_update`, lock the row until the transaction ends."""
        ...

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> Invitation | None: ...

    async def save(self, invitation: Invitation) -> None:
        """Persist `token_hash`, `accepted_at` and `revoked_at`."""
        ...

    async def revoke_open_for_email(self, email: Email, *, now: datetime) -> None:
        """Set `revoked_at` on every invitation for the email that is neither accepted nor
        revoked."""
        ...


class PasswordResetRepository(Protocol):
    """Write side. Joins the active transaction."""

    async def add(self, reset: PasswordReset) -> None: ...

    async def get(self, reset_id: UUID, *, for_update: bool = False) -> PasswordReset | None:
        """With `for_update`, lock the row until the transaction ends."""
        ...

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> PasswordReset | None: ...

    async def save(self, reset: PasswordReset) -> None:
        """Persist `token_hash`, `used_at` and `cancelled_at`."""
        ...

    async def cancel_open_for_user(self, user_id: UUID, *, now: datetime) -> None:
        """Set `cancelled_at` on every reset of the user that is neither used nor cancelled."""
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
