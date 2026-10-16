"""The real container resolves the orders services without connecting to anything."""

import pytest

from fragancia_api.container import MODULES, build_container
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from tests.support import make_settings


def test_orders_is_registered_after_inventory() -> None:
    names = [module.name for module in MODULES]

    assert "orders" in names
    assert names.index("orders") > names.index("inventory")


@pytest.mark.parametrize(
    "service", [UpdateDeliveryOptions, GetDeliveryOptions, GetPublicDeliveryOptions, QuoteDelivery]
)
async def test_the_container_resolves_every_orders_service(service: type) -> None:
    container = build_container(make_settings())

    assert isinstance(container.services.get(service), service)
    await container.close()
