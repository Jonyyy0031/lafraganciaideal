"""Wiring of the catalog module for the composition root."""

from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.http.router import routers
from fragancia_api.modules.catalog.infrastructure.sql_brand_queries import SqlBrandQueries
from fragancia_api.modules.catalog.infrastructure.sql_brand_repository import SqlBrandRepository
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform


def register(platform: Platform, services: ServiceRegistry) -> None:
    queries = SqlBrandQueries(platform.database)
    services.add(
        CreateBrand,
        CreateBrand(
            brands=SqlBrandRepository(platform.database),
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    services.add(ListPublicBrands, ListPublicBrands(queries))
    services.add(ListAdminBrands, ListAdminBrands(queries))


module = AppModule(name="catalog", register=register, routers=routers)
