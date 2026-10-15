from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.dialects import postgresql

from fragancia_api.modules.orders.domain.delivery import (
    DeliveryPrice,
    DeliverySettings,
    HomeDelivery,
    LocalDelivery,
    Pickup,
    PostalCode,
)
from fragancia_api.modules.orders.infrastructure.tables import delivery_settings
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Money

SETTINGS_ROW_ID = 1


def _to_values(settings: DeliverySettings) -> dict[str, Any]:
    return {
        "home_enabled": settings.home.enabled,
        "home_price_cents": settings.home.price.money.cents,
        "home_free_from_cents": (
            settings.home.free_from.cents if settings.home.free_from is not None else None
        ),
        "local_enabled": settings.local.enabled,
        "local_price_cents": settings.local.price.money.cents,
        "local_postal_codes": sorted(code.value for code in settings.local.postal_codes),
        "pickup_enabled": settings.pickup.enabled,
        "pickup_address": settings.pickup.address,
        "pickup_hours": settings.pickup.hours,
    }


def _to_domain(row: Row[Any]) -> DeliverySettings:
    return DeliverySettings(
        home=HomeDelivery(
            enabled=row.home_enabled,
            price=DeliveryPrice(Money(row.home_price_cents)),
            free_from=(
                Money(row.home_free_from_cents) if row.home_free_from_cents is not None else None
            ),
        ),
        local=LocalDelivery(
            enabled=row.local_enabled,
            price=DeliveryPrice(Money(row.local_price_cents)),
            postal_codes=frozenset(PostalCode(value) for value in row.local_postal_codes),
        ),
        pickup=Pickup(
            enabled=row.pickup_enabled,
            address=row.pickup_address,
            hours=row.pickup_hours,
        ),
    )


_SELECT = select(delivery_settings).where(delivery_settings.c.id == SETTINGS_ROW_ID)


class SqlDeliverySettingsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self) -> DeliverySettings:
        row = (await self._database.session.execute(_SELECT)).first()
        return DeliverySettings.default() if row is None else _to_domain(row)

    async def get_for_update(self) -> DeliverySettings:
        session = self._database.session
        # The default row first, so there is always a row to lock.
        await session.execute(
            postgresql.insert(delivery_settings)
            .values(id=SETTINGS_ROW_ID, **_to_values(DeliverySettings.default()))
            .on_conflict_do_nothing(index_elements=[delivery_settings.c.id])
        )
        row = (await session.execute(_SELECT.with_for_update())).one()
        return _to_domain(row)

    async def save(self, settings: DeliverySettings) -> None:
        values = _to_values(settings)
        await self._database.session.execute(
            postgresql.insert(delivery_settings)
            .values(id=SETTINGS_ROW_ID, **values)
            .on_conflict_do_update(index_elements=[delivery_settings.c.id], set_=values)
        )


class SqlDeliverySettingsReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def current(self) -> DeliverySettings:
        async with self._database.reader() as session:
            row = (await session.execute(_SELECT)).first()
        return DeliverySettings.default() if row is None else _to_domain(row)
