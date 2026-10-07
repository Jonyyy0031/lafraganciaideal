from uuid import UUID

from fragancia_api.modules.identity.domain.errors import (
    ActorInactive,
    CannotDeactivateSelf,
    UserNotFound,
)
from fragancia_api.modules.identity.domain.repositories import (
    PasswordResetRepository,
    SessionRepository,
    UserRepository,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class DeactivateUser:
    """Command: deactivate a user, close all their sessions and cancel their open resets.
    Nobody can deactivate themselves, and the actor must still be active when the rows are
    locked, so at least one active owner always remains (even if two owners act at once)."""

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        resets: PasswordResetRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._resets = resets
        self._transactions = transactions
        self._clock = clock

    async def execute(self, actor_id: UUID, user_id: UUID) -> Result[None, DomainError]:
        if actor_id == user_id:
            return Err(CannotDeactivateSelf())

        async def work() -> Result[None, DomainError]:
            now = self._clock.now()
            # Lock the actor's row and the target's in id order (no deadlock between two owners
            # deactivating each other), then re-check the actor: the owner who lost the race is
            # refused, so an active owner always remains.
            locked = {}
            for locked_id in sorted((actor_id, user_id), key=str):
                locked[locked_id] = await self._users.get_for_update(locked_id)
            actor = locked[actor_id]
            if actor is None or not actor.is_active:
                return Err(ActorInactive())
            user = locked[user_id]
            if user is None:
                return Err(UserNotFound())
            user.deactivate()
            await self._users.save(user)
            await self._sessions.revoke_all_for_user(user_id, except_id=None, now=now)
            await self._resets.cancel_open_for_user(user_id, now=now)
            return Ok(None)

        return await self._transactions.run(work)


class ReactivateUser:
    """Command: let a deactivated user sign in again."""

    def __init__(self, *, users: UserRepository, transactions: TransactionRunner) -> None:
        self._users = users
        self._transactions = transactions

    async def execute(self, user_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            user = await self._users.get_for_update(user_id)
            if user is None:
                return Err(UserNotFound())
            user.reactivate()
            await self._users.save(user)
            return Ok(None)

        return await self._transactions.run(work)
