from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fragancia_api.shared.kernel import DomainEvent


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
