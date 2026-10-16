"""The read port of the delivery options lives in the application layer."""

from fragancia_api.modules.orders.application.ports import DeliverySettingsReader
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryDeliverySettings


def test_the_reader_port_is_defined_in_application_ports() -> None:
    assert DeliverySettingsReader.__module__ == "fragancia_api.modules.orders.application.ports"


async def test_the_in_memory_adapter_satisfies_the_reader_port() -> None:
    reader: DeliverySettingsReader = InMemoryDeliverySettings()

    assert await reader.current() == DeliverySettings.default()
