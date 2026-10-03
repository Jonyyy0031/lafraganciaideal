from uuid import UUID

from fragancia_api.modules.identity.application.ports import PasswordHasher
from fragancia_api.modules.identity.domain.errors import CurrentPasswordWrong
from fragancia_api.modules.identity.domain.repositories import SessionRepository, UserRepository
from fragancia_api.modules.identity.domain.user import PlainPassword, User
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class ChangePassword:
    """Command: change my password. Every other session of mine is revoked; the current one
    keeps working."""

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        hasher: PasswordHasher,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._hasher = hasher
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, user_id: UUID, session_id: UUID, current: str, new: str
    ) -> Result[None, DomainError]:
        match PlainPassword.create(new):
            case Err(weak):
                return Err(weak)
            case Ok(plain):
                pass

        async def load() -> Result[User | None, DomainError]:
            return Ok(await self._users.get(user_id))

        match await self._transactions.run(load):
            case Err(error):
                return Err(error)
            case Ok(found):
                pass
        if found is None:
            # The caller comes from a resolved session, whose user always exists.
            raise LookupError(f"User {user_id} not found")
        user = found
        if not self._hasher.verify(user.password_hash, current):
            return Err(CurrentPasswordWrong())

        new_hash = self._hasher.hash(plain.value)  # slow: outside any transaction

        async def work() -> Result[None, DomainError]:
            now = self._clock.now()
            user.change_password(new_hash, now=now)
            await self._users.save(user)
            await self._sessions.revoke_all_for_user(user_id, except_id=session_id, now=now)
            return Ok(None)

        return await self._transactions.run(work)
