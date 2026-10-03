from uuid import UUID

from fragancia_api.modules.identity.domain.errors import SessionNotFound
from fragancia_api.modules.identity.domain.repositories import SessionRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class RevokeSession:
    """Command: close one of my sessions. Another user's session is "not found"."""

    def __init__(
        self, *, sessions: SessionRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._sessions = sessions
        self._transactions = transactions
        self._clock = clock

    async def execute(self, user_id: UUID, session_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            session = await self._sessions.get(session_id)
            if session is None or session.user_id != user_id or session.revoked_at is not None:
                return Err(SessionNotFound())
            session.revoke(self._clock.now())
            await self._sessions.save(session)
            return Ok(None)

        return await self._transactions.run(work)


class RevokeOtherSessions:
    """Command: close every session of mine except the current one."""

    def __init__(
        self, *, sessions: SessionRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._sessions = sessions
        self._transactions = transactions
        self._clock = clock

    async def execute(self, user_id: UUID, current_session_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            await self._sessions.revoke_all_for_user(
                user_id, except_id=current_session_id, now=self._clock.now()
            )
            return Ok(None)

        return await self._transactions.run(work)
