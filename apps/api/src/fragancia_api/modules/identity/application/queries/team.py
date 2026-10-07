from fragancia_api.modules.identity.application.ports import AccountQueries
from fragancia_api.modules.identity.contracts import AdminInvitation, AdminUser
from fragancia_api.shared.application.clock import Clock


class ListUsers:
    """Query: every back-office user."""

    def __init__(self, queries: AccountQueries) -> None:
        self._queries = queries

    async def execute(self) -> list[AdminUser]:
        return await self._queries.users()


class ListPendingInvitations:
    """Query: invitations that can still be accepted, newest first."""

    def __init__(self, queries: AccountQueries, *, clock: Clock) -> None:
        self._queries = queries
        self._clock = clock

    async def execute(self) -> list[AdminInvitation]:
        return await self._queries.pending_invitations(self._clock.now())
