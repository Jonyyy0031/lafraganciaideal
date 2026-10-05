"""A back-office session: opaque on the wire, only the token's hash is kept.

A session is valid while it is not revoked, before its absolute expiry and while it has been
seen within the idle timeout.
"""

from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.shared.kernel import new_id

USER_AGENT_MAX_LENGTH = 255


class Session:
    def __init__(
        self,
        *,
        id: UUID,
        user_id: UUID,
        token_hash: str,
        created_at: datetime,
        last_seen_at: datetime,
        expires_at: datetime,
        revoked_at: datetime | None,
        user_agent: str | None,
        ip: str | None,
    ) -> None:
        self.id = id
        self.user_id = user_id
        self.token_hash = token_hash
        self.created_at = created_at
        self.last_seen_at = last_seen_at
        self.expires_at = expires_at
        self.revoked_at = revoked_at
        self.user_agent = user_agent
        self.ip = ip

    @classmethod
    def open(
        cls,
        user_id: UUID,
        token_hash: str,
        *,
        now: datetime,
        max_age: timedelta,
        user_agent: str | None,
        ip: str | None,
    ) -> Session:
        return cls(
            id=new_id(),
            user_id=user_id,
            token_hash=token_hash,
            created_at=now,
            last_seen_at=now,
            expires_at=now + max_age,
            revoked_at=None,
            user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent is not None else None,
            ip=ip,
        )

    def is_valid(self, now: datetime, idle: timedelta) -> bool:
        return self.revoked_at is None and now < self.expires_at and now < self.last_seen_at + idle

    def revoke(self, now: datetime) -> None:
        if self.revoked_at is None:
            self.revoked_at = now

    def touch(self, now: datetime) -> None:
        self.last_seen_at = now
