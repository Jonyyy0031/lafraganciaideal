"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from fragancia_api.modules.orders.domain.delivery import DeliverySettings


class InMemoryDeliverySettings:
    """The `DeliverySettingsRepository` and `DeliverySettingsReader` over one value."""

    def __init__(self, settings: DeliverySettings | None = None) -> None:
        self.settings = settings if settings is not None else DeliverySettings.default()

    async def get(self) -> DeliverySettings:
        return self.settings

    async def get_for_update(self) -> DeliverySettings:
        return self.settings

    async def save(self, settings: DeliverySettings) -> None:
        self.settings = settings

    async def current(self) -> DeliverySettings:
        return self.settings
