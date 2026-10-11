"""What a business module hands to the composition root."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from fastapi import APIRouter

from fragancia_api.config import Settings
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.email import EmailSender
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
    email: EmailSender
    subscriptions: EventSubscriptions
    clock: Clock
    settings: Settings


@dataclass(frozen=True)
class ScheduledJob:
    """A periodic job the worker runs as an arq cron named `cron:<name>`. `run` gets the
    service registry and returns how many items it processed. `every_seconds` divides 60, or
    is 60 (once a minute, at second 0)."""

    name: str
    run: Callable[[ServiceRegistry], Awaitable[int]]
    every_seconds: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not 0 < self.every_seconds <= 60 or 60 % self.every_seconds != 0:
            raise ValueError(f"Job {self.name}: every_seconds must divide 60")


@dataclass(frozen=True)
class AppModule:
    """`register` builds the module's adapters and use cases into the registry (and may
    subscribe to events); `routers` are mounted under /api/v1; `jobs` run in the worker."""

    name: str
    register: Callable[[Platform, ServiceRegistry], None]
    routers: Sequence[APIRouter] = field(default_factory=tuple)
    jobs: Sequence[ScheduledJob] = field(default_factory=tuple)
