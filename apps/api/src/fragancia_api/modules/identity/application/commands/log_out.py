from uuid import UUID

from fragancia_api.modules.identity.domain.repositories import SessionRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Ok, Result


class LogOut:
    """Command: revoke the current session. Always succeeds (idempotent)."""

    def __init__(
        self, *, sessions: SessionRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._sessions = sessions
        self._transactions = transactions
        self._clock = clock

    async def execute(self, session_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            session = await self._sessions.get(session_id)
            if session is not None:
                session.revoke(self._clock.now())
                await self._sessions.save(session)
            return Ok(None)

        return await self._transactions.run(work)
