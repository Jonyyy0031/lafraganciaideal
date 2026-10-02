"""Transactional outbox (ADR 0006).

`OutboxEventPublisher` stores events in `platform.outbox` inside the active transaction, so an
event exists if and only if the change that produced it was committed. `OutboxRelay` (run by
the worker) delivers each pending row to its subscribers in its own transaction and marks it
published; a failure is recorded and retried later with backoff. Delivery is at-least-once.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Uuid

from fragancia_api.shared.application.events import EventHandler, EventMessage
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.infrastructure.tables import metadata
from fragancia_api.shared.kernel import DomainEvent

log = structlog.get_logger(__name__)

MAX_BACKOFF = timedelta(minutes=5)

outbox = Table(
    "outbox",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("published_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Index("ix_outbox_pending", "available_at", postgresql_where="published_at IS NULL"),
    schema="platform",
)


class OutboxEventPublisher:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def publish(self, events: Sequence[DomainEvent]) -> None:
        if not events:
            return
        await self._database.session.execute(
            insert(outbox),
            [
                {
                    "id": event.event_id,
                    "name": event.name,
                    "payload": event.payload(),
                    "occurred_at": event.occurred_at,
                }
                for event in events
            ],
        )


class EventBus:
    """In-process registry of subscribers by event name."""

    def __init__(self) -> None:
        self._handlers: defaultdict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name].append(handler)

    def handlers(self, event_name: str) -> list[EventHandler]:
        return list(self._handlers.get(event_name, []))


class OutboxRelay:
    def __init__(self, database: Database, bus: EventBus, batch_size: int = 50) -> None:
        self._database = database
        self._bus = bus
        self._batch_size = batch_size

    async def relay_batch(self) -> int:
        """Deliver up to `batch_size` due events. Returns how many rows were processed."""
        processed = 0
        while processed < self._batch_size and await self._relay_one():
            processed += 1
        return processed

    async def _relay_one(self) -> bool:
        async with self._database.unit_of_work() as session:
            row = (
                await session.execute(
                    select(outbox)
                    .where(outbox.c.published_at.is_(None), outbox.c.available_at <= func.now())
                    .order_by(outbox.c.occurred_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).one_or_none()
            if row is None:
                await session.rollback()
                return False
            message = EventMessage(
                id=row.id, name=row.name, payload=row.payload, occurred_at=row.occurred_at
            )
            try:
                for handler in self._bus.handlers(message.name):
                    await handler(message)
                await session.execute(
                    update(outbox).where(outbox.c.id == row.id).values(published_at=func.now())
                )
                await session.commit()
            except Exception as error:
                await session.rollback()
                await self._record_failure(row, error)
            return True

    async def _record_failure(self, row: Any, error: Exception) -> None:
        attempts = row.attempts + 1
        delay = min(timedelta(seconds=2**attempts), MAX_BACKOFF)
        log.warning(
            "outbox.delivery_failed",
            event_id=str(row.id),
            event_name=row.name,
            attempts=attempts,
            error=repr(error),
        )
        async with self._database.unit_of_work() as session:
            await session.execute(
                update(outbox)
                .where(outbox.c.id == row.id)
                .values(
                    attempts=attempts,
                    last_error=repr(error)[:2000],
                    available_at=datetime.now(UTC) + delay,
                )
            )
            await session.commit()
