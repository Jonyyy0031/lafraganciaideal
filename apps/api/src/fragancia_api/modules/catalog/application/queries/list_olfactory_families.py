from fragancia_api.modules.catalog.application.ports import OlfactoryFamilyQueries
from fragancia_api.modules.catalog.contracts import AdminOlfactoryFamilyPage, PublicOlfactoryFamily


class ListPublicOlfactoryFamilies:
    """Query: active olfactory families for the storefront."""

    def __init__(self, queries: OlfactoryFamilyQueries) -> None:
        self._queries = queries

    async def execute(self) -> list[PublicOlfactoryFamily]:
        return await self._queries.list_active()


class ListAdminOlfactoryFamilies:
    """Query: every olfactory family for the back office, paginated."""

    def __init__(self, queries: OlfactoryFamilyQueries) -> None:
        self._queries = queries

    async def execute(self, *, page: int, size: int) -> AdminOlfactoryFamilyPage:
        return await self._queries.list_all(page=page, size=size)
