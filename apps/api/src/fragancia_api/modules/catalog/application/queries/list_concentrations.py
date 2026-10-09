from fragancia_api.modules.catalog.application.ports import ConcentrationQueries
from fragancia_api.modules.catalog.contracts import AdminConcentrationPage, PublicConcentration


class ListPublicConcentrations:
    """Query: active concentrations for the storefront."""

    def __init__(self, queries: ConcentrationQueries) -> None:
        self._queries = queries

    async def execute(self) -> list[PublicConcentration]:
        return await self._queries.list_active()


class ListAdminConcentrations:
    """Query: every concentration for the back office, paginated."""

    def __init__(self, queries: ConcentrationQueries) -> None:
        self._queries = queries

    async def execute(self, *, page: int, size: int) -> AdminConcentrationPage:
        return await self._queries.list_all(page=page, size=size)
