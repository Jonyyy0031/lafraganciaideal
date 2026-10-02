from fragancia_api.shared.kernel.events import DomainEvent


class AggregateRoot:
    """Consistency boundary. Records domain events that the use case publishes after saving."""

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def record(self, event: DomainEvent) -> None:
        self._events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        """Return the recorded events and forget them (each event is published once)."""
        events, self._events = self._events, []
        return events
