"""Read-side ports of the orders module (queries run outside any transaction)."""

from typing import Protocol

from fragancia_api.modules.orders.domain.delivery import DeliverySettings


class DeliverySettingsReader(Protocol):
    """Read side: the settings outside any transaction (`Database.reader()`)."""

    async def current(self) -> DeliverySettings:
        """The delivery settings (the default when nothing is stored)."""
        ...
