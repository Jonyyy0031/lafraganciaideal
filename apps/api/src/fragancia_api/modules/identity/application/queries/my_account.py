from uuid import UUID

from fragancia_api.modules.identity.application.policy import AuthPolicy
from fragancia_api.modules.identity.application.ports import AccountQueries
from fragancia_api.modules.identity.contracts import AdminMe, AdminSession
from fragancia_api.shared.application.clock import Clock


class GetMyAccount:
    """Query: the signed-in user with their role and permissions."""

    def __init__(self, queries: AccountQueries) -> None:
        self._queries = queries

    async def execute(self, user_id: UUID) -> AdminMe | None:
        return await self._queries.me(user_id)


class ListMySessions:
    """Query: my valid sessions, most recently seen first, marking the current one."""

    def __init__(self, queries: AccountQueries, *, clock: Clock, policy: AuthPolicy) -> None:
        self._queries = queries
        self._clock = clock
        self._policy = policy

    async def execute(self, user_id: UUID, *, current_session_id: UUID) -> list[AdminSession]:
        return await self._queries.sessions(
            user_id,
            current_session_id=current_session_id,
            now=self._clock.now(),
            idle=self._policy.session_idle,
        )
