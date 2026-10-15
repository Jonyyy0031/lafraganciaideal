"""Wiring of the orders module for the composition root."""

from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.modules.orders.infrastructure.sql_delivery_settings_repository import (
    SqlDeliverySettingsReader,
    SqlDeliverySettingsRepository,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform


def register(platform: Platform, services: ServiceRegistry) -> None:
    repository = SqlDeliverySettingsRepository(platform.database)
    reader = SqlDeliverySettingsReader(platform.database)

    services.add(
        UpdateDeliveryOptions,
        UpdateDeliveryOptions(repository=repository, transactions=platform.transactions),
    )
    services.add(GetDeliveryOptions, GetDeliveryOptions(reader))
    services.add(GetPublicDeliveryOptions, GetPublicDeliveryOptions(reader))
    services.add(QuoteDelivery, QuoteDelivery(reader))


module = AppModule(name="orders", register=register, routers=routers)
