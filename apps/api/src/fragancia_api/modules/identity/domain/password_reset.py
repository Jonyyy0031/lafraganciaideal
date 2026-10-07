"""A password-reset request. The link's token is minted when the email is sent; the record
stores only its digest."""

from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.events import PasswordResetRequested
from fragancia_api.shared.kernel import AggregateRoot, new_id


class PasswordReset(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        user_id: UUID,
        created_at: datetime,
        expires_at: datetime,
        token_hash: str | None,
        used_at: datetime | None,
        cancelled_at: datetime | None,
    ) -> None:
        super().__init__()
        self.id = id
        self.user_id = user_id
        self.created_at = created_at
        self.expires_at = expires_at
        self.token_hash = token_hash
        self.used_at = used_at
        self.cancelled_at = cancelled_at

    @classmethod
    def request(cls, user_id: UUID, *, now: datetime, ttl: timedelta) -> PasswordReset:
        reset = cls(
            id=new_id(),
            user_id=user_id,
            created_at=now,
            expires_at=now + ttl,
            token_hash=None,
            used_at=None,
            cancelled_at=None,
        )
        reset.record(PasswordResetRequested(reset_id=reset.id))
        return reset

    def is_pending(self, now: datetime) -> bool:
        return self.used_at is None and self.cancelled_at is None and now < self.expires_at

    def attach_token(self, token_hash: str) -> None:
        self.token_hash = token_hash

    def use(self, now: datetime) -> None:
        if self.used_at is None and self.cancelled_at is None:
            self.used_at = now
