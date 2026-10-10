from dataclasses import replace
from uuid import UUID

from fragancia_api.modules.catalog.application.ports import PerfumeQueries, PublicPerfumeFilters
from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    PublicPerfume,
    PublicPerfumePage,
)
from fragancia_api.modules.catalog.domain.errors import PerfumeNotFound
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.kernel import Err, Ok, Result


class ListAdminPerfumes:
    """Query: the back-office perfume list, paginated; archived ones only on request."""

    def __init__(self, queries: PerfumeQueries) -> None:
        self._queries = queries

    async def execute(self, *, page: int, size: int, archived: bool) -> AdminPerfumePage:
        return await self._queries.list_admin(page=page, size=size, archived=archived)


class ListPublicPerfumes:
    """Query: the public catalog, filtered, sorted and paginated."""

    def __init__(self, queries: PerfumeQueries, clock: Clock) -> None:
        self._queries = queries
        self._clock = clock

    async def execute(self, filters: PublicPerfumeFilters) -> PublicPerfumePage:
        q = filters.q.strip() if filters.q is not None else None
        filters = replace(filters, q=q or None)
        return await self._queries.list_public(filters, now=self._clock.now())


class GetPublicPerfume:
    """Query: one visible perfume by slug (current or retired), with its active presentations."""

    def __init__(self, queries: PerfumeQueries, clock: Clock) -> None:
        self._queries = queries
        self._clock = clock

    async def execute(self, slug: str) -> Result[PublicPerfume, PerfumeNotFound]:
        perfume = await self._queries.get_public(slug, now=self._clock.now())
        if perfume is None:
            return Err(PerfumeNotFound())
        return Ok(perfume)


class GetAdminPerfume:
    """Query: one perfume with its presentations, for the back office."""

    def __init__(self, queries: PerfumeQueries) -> None:
        self._queries = queries

    async def execute(self, perfume_id: UUID) -> Result[AdminPerfume, PerfumeNotFound]:
        perfume = await self._queries.get_admin(perfume_id)
        if perfume is None:
            return Err(PerfumeNotFound())
        return Ok(perfume)
