"""Domain events: immutable facts recorded by aggregates and published through the outbox."""

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar
from uuid import UUID

from fragancia_api.shared.kernel.ids import new_id


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Base class. Subclasses set `name` as `<module>.<aggregate>.<past-tense verb>`:

    @dataclass(frozen=True, kw_only=True)
    class BrandCreated(DomainEvent):
        name: ClassVar[str] = "catalog.brand.created"
        brand_id: UUID
    """

    name: ClassVar[str]
    event_id: UUID = field(default_factory=new_id)
    occurred_at: datetime = field(default_factory=_now)

    def payload(self) -> dict[str, object]:
        """The event's own fields as JSON-compatible values (envelope fields excluded)."""
        return {
            f.name: _to_json(getattr(self, f.name))
            for f in dataclasses.fields(self)
            if f.name not in ("event_id", "occurred_at")
        }


def _to_json(value: object) -> object:
    match value:
        case UUID() | datetime():
            return str(value) if isinstance(value, UUID) else value.isoformat()
        case Enum():
            return _to_json(value.value)
        case list() | tuple():
            return [_to_json(item) for item in value]
        case dict():
            return {str(key): _to_json(item) for key, item in value.items()}
        case _:
            return value
