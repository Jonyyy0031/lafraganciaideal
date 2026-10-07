from fragancia_api.modules.identity.application.policy import TOUCH_INTERVAL, AuthPolicy
from fragancia_api.modules.identity.application.ports import SessionTokens
from fragancia_api.modules.identity.domain.repositories import SessionRepository, UserRepository
from fragancia_api.shared.application.actor import Actor
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Ok, Result


class ResolveSessionActor:
    """The `ActorResolver` behind `require_admin`: a session token → its active user.

    Unknown, revoked, expired or idle sessions and inactive users resolve to None. The
    session's `last_seen_at` is refreshed at most once per `TOUCH_INTERVAL`.
    """

    def __init__(
        self,
        *,
        sessions: SessionRepository,
        users: UserRepository,
        tokens: SessionTokens,
        transactions: TransactionRunner,
        clock: Clock,
        policy: AuthPolicy,
    ) -> None:
        self._sessions = sessions
        self._users = users
        self._tokens = tokens
        self._transactions = transactions
        self._clock = clock
        self._policy = policy

    async def resolve(self, token: str) -> Actor | None:
        async def work() -> Result[Actor | None, DomainError]:
            now = self._clock.now()
            session = await self._sessions.get_by_token_hash(self._tokens.digest(token))
            if session is None or not session.is_valid(now, self._policy.session_idle):
                return Ok(None)
            user = await self._users.get(session.user_id)
            if user is None or not user.is_active:
                return Ok(None)
            if now - session.last_seen_at >= TOUCH_INTERVAL:
                session.touch(now)
                await self._sessions.save(session)
            return Ok(
                Actor(
                    id=str(user.id),
                    is_admin=True,
                    session_id=session.id,
                    permissions=user.permissions,
                )
            )

        match await self._transactions.run(work):
            case Ok(actor):
                return actor
            case _:
                return None
