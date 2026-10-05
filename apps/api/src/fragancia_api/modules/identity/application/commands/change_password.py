from uuid import UUID

from fragancia_api.modules.identity.application.policy import AuthPolicy
from fragancia_api.modules.identity.application.ports import LoginThrottle, PasswordHasher
from fragancia_api.modules.identity.domain.errors import CurrentPasswordWrong, TooManyAttempts
from fragancia_api.modules.identity.domain.repositories import SessionRepository, UserRepository
from fragancia_api.modules.identity.domain.user import PlainPassword, User
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class ChangePassword:
    """Command: change my password. Every other session of mine is revoked; the current one
    keeps working.

    Checking the current password is throttled like a sign-in: the attempt is counted under
    `password:<user_id>` BEFORE the check, with the per-email limit, and a correct password
    clears the count. So a stolen session cannot brute-force the account password.
    """

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        throttle: LoginThrottle,
        hasher: PasswordHasher,
        transactions: TransactionRunner,
        clock: Clock,
        policy: AuthPolicy,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._throttle = throttle
        self._hasher = hasher
        self._transactions = transactions
        self._clock = clock
        self._policy = policy

    async def execute(
        self, user_id: UUID, session_id: UUID, current: str, new: str
    ) -> Result[None, DomainError]:
        match PlainPassword.create(new):
            case Err(weak):
                return Err(weak)
            case Ok(plain):
                pass
        throttle_key = f"password:{user_id}"

        async def count_and_load() -> Result[tuple[int, User | None], DomainError]:
            attempts = await self._throttle.hit(
                throttle_key, now=self._clock.now(), window=self._policy.throttle_window
            )
            return Ok((attempts, await self._users.get(user_id)))  # always Ok: the count commits

        match await self._transactions.run(count_and_load):
            case Err(error):
                return Err(error)
            case Ok((attempts, found)):
                pass
        if attempts > self._policy.email_max_attempts:
            return Err(TooManyAttempts())
        if found is None:
            # The caller comes from a resolved session, whose user always exists.
            raise LookupError(f"User {user_id} not found")
        user = found
        if not await self._hasher.verify(user.password_hash, current):
            return Err(CurrentPasswordWrong())  # the attempt stays counted

        new_hash = await self._hasher.hash(plain.value)  # slow: outside any transaction

        async def work() -> Result[None, DomainError]:
            now = self._clock.now()
            user.change_password(new_hash, now=now)
            await self._users.save(user)
            await self._sessions.revoke_all_for_user(user_id, except_id=session_id, now=now)
            await self._throttle.clear(throttle_key)
            return Ok(None)

        return await self._transactions.run(work)
