from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.shared.contracts.outbox import FailedEventPage
from fragancia_api.shared.kernel import DomainEvent, NotFoundError


class EventPublisher(Protocol):
    """Publishes domain events atomically with the current transaction (outbox)."""

    async def publish(self, events: Sequence[DomainEvent]) -> None: ...


@dataclass(frozen=True, slots=True)
class EventMessage:
    """What a subscriber receives: the event as stored, decoupled from the producer's class."""

    id: UUID
    name: str
    payload: dict[str, object]
    occurred_at: datetime


type EventHandler = Callable[[EventMessage], Awaitable[None]]


class EventSubscriptions(Protocol):
    """Where modules register what they react to. Handlers must be idempotent: delivery is
    at-least-once."""

    def subscribe(self, event_name: str, handler: EventHandler) -> None: ...


@dataclass(frozen=True)
class OutboxEventNotFound(NotFoundError):
    code: str = "OUTBOX_EVENT_NOT_FOUND"
    message: str = "No failed event with this id"


class OutboxAdmin(Protocol):
    """The panel's view of events that exhausted their retries."""

    async def failed(self, *, page: int, size: int) -> FailedEventPage:
        """Failed events, newest `failed_at` first (ties by id)."""
        ...

    async def retry(self, event_id: UUID) -> bool:
        """Give a failed event a fresh retry window. False when no failed event has that id."""
        ...
