"""What a business module hands to the composition root."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from fastapi import APIRouter

from fragancia_api.config import Settings
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher, EventSubscriptions
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.database import Database


@dataclass(frozen=True)
class Platform:
    """Shared adapters a module may use to build its own, plus the application settings
    (a module reads only the fields it owns, e.g. identity's session lifetimes)."""

    database: Database
    transactions: TransactionRunner
    events: EventPublisher
    subscriptions: EventSubscriptions
    clock: Clock
    settings: Settings


@dataclass(frozen=True)
class AppModule:
    """`register` builds the module's adapters and use cases into the registry (and may
    subscribe to events); `routers` are mounted under /api/v1."""

    name: str
    register: Callable[[Platform, ServiceRegistry], None]
    routers: Sequence[APIRouter] = field(default_factory=tuple)
