from fragancia_api.modules.catalog.application.ports import BrandQueries
from fragancia_api.modules.catalog.contracts import AdminBrand, PublicBrand
from fragancia_api.shared.contracts import Page


class ListPublicBrands:
    """Query: active brands for the storefront."""

    def __init__(self, queries: BrandQueries) -> None:
        self._queries = queries

    async def execute(self) -> list[PublicBrand]:
        return await self._queries.list_active()


class ListAdminBrands:
    """Query: every brand for the back office, paginated."""

    def __init__(self, queries: BrandQueries) -> None:
        self._queries = queries

    async def execute(self, *, page: int, size: int) -> Page[AdminBrand]:
        return await self._queries.list_all(page=page, size=size)
