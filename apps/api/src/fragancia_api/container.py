"""Composition root: the ONLY place that knows concrete adapters and wires modules.

Adding a module = import its `module` and append it to `MODULES`.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import APIRouter

from fragancia_api.config import Settings
from fragancia_api.modules.catalog.module import module as catalog
from fragancia_api.modules.identity.module import module as identity
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import Database, SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import (
    EventBus,
    OutboxEventPublisher,
    OutboxRelay,
)
from fragancia_api.shared.infrastructure.tables import metadata
from fragancia_api.shared.infrastructure.valkey import ValkeyHealth
from fragancia_api.shared.module import AppModule, Platform

MODULES: Sequence[AppModule] = (identity, catalog)

__all__ = ["MODULES", "Container", "build_container", "metadata"]


@dataclass(frozen=True)
class Container:
    settings: Settings
    database: Database
    services: ServiceRegistry
    routers: Sequence[APIRouter]
    relay: OutboxRelay
    valkey: ValkeyHealth

    async def close(self) -> None:
        await self.valkey.close()
        await self.database.dispose()


def build_container(settings: Settings, modules: Sequence[AppModule] = MODULES) -> Container:
    database = Database(settings.database_url)
    bus = EventBus()
    platform = Platform(
        database=database,
        transactions=SqlTransactionRunner(database),
        events=OutboxEventPublisher(database),
        subscriptions=bus,
        clock=SystemClock(),
        settings=settings,
    )
    valkey = ValkeyHealth(settings.valkey_url)

    services = ServiceRegistry()
    services.add(TransactionRunner, platform.transactions)  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks(database=database.ping, valkey=valkey.ping))
    for module in modules:
        module.register(platform, services)
    if ActorResolver not in services:
        raise RuntimeError("No module registered the ActorResolver")

    return Container(
        settings=settings,
        database=database,
        services=services,
        routers=[router for module in modules for router in module.routers],
        relay=OutboxRelay(database, bus),
        valkey=valkey,
    )
