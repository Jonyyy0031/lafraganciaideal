from uuid import UUID

from fragancia_api.modules.catalog.application.ports import PerfumeQueries
from fragancia_api.modules.catalog.contracts import AdminPerfume, AdminPerfumePage
from fragancia_api.modules.catalog.domain.errors import PerfumeNotFound
from fragancia_api.shared.kernel import Err, Ok, Result


class ListAdminPerfumes:
    """Query: the back-office perfume list, paginated; archived ones only on request."""

    def __init__(self, queries: PerfumeQueries) -> None:
        self._queries = queries

    async def execute(self, *, page: int, size: int, archived: bool) -> AdminPerfumePage:
        return await self._queries.list_admin(page=page, size=size, archived=archived)


class GetAdminPerfume:
    """Query: one perfume with its presentations, for the back office."""

    def __init__(self, queries: PerfumeQueries) -> None:
        self._queries = queries

    async def execute(self, perfume_id: UUID) -> Result[AdminPerfume, PerfumeNotFound]:
        perfume = await self._queries.get_admin(perfume_id)
        if perfume is None:
            return Err(PerfumeNotFound())
        return Ok(perfume)
