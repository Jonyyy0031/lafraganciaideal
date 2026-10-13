"""Wiring of the inventory module for the composition root."""

from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.application.queries.stock import (
    GetInventorySettings,
    GetStock,
    ListMovements,
    ListStock,
)
from fragancia_api.modules.inventory.http.router import routers
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_queries import SqlStockQueries
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform


def register(platform: Platform, services: ServiceRegistry) -> None:
    stock = SqlStockRepository(platform.database)
    settings = SqlSettingsRepository(platform.database)
    queries = SqlStockQueries(platform.database)
    # The catalog registers before inventory (MODULES order), so its facade is available.
    catalog = CatalogPresentations(services.get(PresentationDirectory))

    services.add(
        RecordMovement,
        RecordMovement(
            stock=stock,
            settings=settings,
            catalog=catalog,
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    services.add(
        SetThreshold,
        SetThreshold(
            stock=stock,
            catalog=catalog,
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    services.add(
        UpdateInventorySettings,
        UpdateInventorySettings(settings=settings, transactions=platform.transactions),
    )
    services.add(GetStock, GetStock(catalog, queries))
    services.add(ListMovements, ListMovements(catalog, queries))
    services.add(ListStock, ListStock(catalog, queries))
    services.add(GetInventorySettings, GetInventorySettings(queries))


module = AppModule(name="inventory", register=register, routers=routers)
