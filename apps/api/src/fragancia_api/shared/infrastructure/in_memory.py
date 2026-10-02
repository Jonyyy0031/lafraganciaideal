"""In-memory stand-ins for the platform ports, used by unit tests of every module."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from fragancia_api.shared.kernel import DomainEvent, Result


class InMemoryTransactionRunner:
    """Runs the work directly. Fakes keep no rollback: use cases validate before writing."""

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        return await work()


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, events: Sequence[DomainEvent]) -> None:
        self.published.extend(events)


class FixedClock:
    def __init__(self, now: datetime = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current
