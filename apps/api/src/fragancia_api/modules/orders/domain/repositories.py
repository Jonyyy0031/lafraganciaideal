from typing import Protocol

from fragancia_api.modules.orders.domain.delivery import DeliverySettings


class DeliverySettingsRepository(Protocol):
    """Write side. Joins the active transaction."""

    async def get(self) -> DeliverySettings:
        """The delivery settings (the default when nothing is stored)."""
        ...

    async def get_for_update(self) -> DeliverySettings:
        """The settings with their row locked `FOR UPDATE` until the transaction ends. The SQL
        adapter first inserts the default row when none is stored, so there is always a row to
        lock."""
        ...

    async def save(self, settings: DeliverySettings) -> None: ...


class DeliverySettingsReader(Protocol):
    """Read side: the settings outside any transaction (`Database.reader()`)."""

    async def current(self) -> DeliverySettings:
        """The delivery settings (the default when nothing is stored)."""
        ...
