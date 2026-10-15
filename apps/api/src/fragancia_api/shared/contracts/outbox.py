"""Panel view of outbox events that exhausted their retries."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from fragancia_api.shared.contracts import Page


class FailedEvent(BaseModel):
    id: UUID
    name: str
    occurred_at: datetime
    failed_at: datetime
    attempts: int
    last_error: str | None


class FailedEventPage(Page[FailedEvent]): ...
