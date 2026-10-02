from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from fragancia_api.container import Container
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import (
    EventBus,
    OutboxEventPublisher,
    OutboxRelay,
    outbox,
)
from fragancia_api.shared.kernel import ConflictError, Err, Ok, new_id
from tests.support import SomethingHappened

pytestmark = pytest.mark.integration


def _event(label: str = "x") -> SomethingHappened:
    return SomethingHappened(thing_id=new_id(), label=label)


async def _publish(container: Container, *events: SomethingHappened, fail: bool = False) -> None:
    runner = SqlTransactionRunner(container.database)
    publisher = OutboxEventPublisher(container.database)

    async def work() -> Ok[None] | Err[ConflictError]:
        await publisher.publish(events)
        return Err(ConflictError("X", "x")) if fail else Ok(None)

    await runner.run(work)


async def _rows(container: Container) -> list[dict[str, object]]:
    async with container.database.reader() as session:
        rows = (await session.execute(select(outbox).order_by(outbox.c.occurred_at))).mappings()
        return [dict(row) for row in rows]


async def test_ok_commits_the_events(container: Container) -> None:
    event = _event()
    await _publish(container, event)

    [row] = await _rows(container)
    assert row["id"] == event.event_id
    assert row["name"] == "tests.something.happened"
    assert row["payload"] == {"thing_id": str(event.thing_id), "label": "x"}
    assert row["published_at"] is None


async def test_err_rolls_back_the_events(container: Container) -> None:
    await _publish(container, _event(), fail=True)
    assert await _rows(container) == []


async def test_an_exception_rolls_back_and_propagates(container: Container) -> None:
    runner = SqlTransactionRunner(container.database)
    publisher = OutboxEventPublisher(container.database)

    async def work() -> Ok[None]:
        await publisher.publish([_event()])
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run(work)
    assert await _rows(container) == []


async def test_nested_runs_join_the_outer_transaction(container: Container) -> None:
    runner = SqlTransactionRunner(container.database)
    publisher = OutboxEventPublisher(container.database)

    async def inner() -> Ok[None]:
        await publisher.publish([_event("inner")])
        return Ok(None)

    async def outer() -> Err[ConflictError]:
        await runner.run(inner)
        return Err(ConflictError("X", "x"))  # the outer failure discards the inner write

    await runner.run(outer)
    assert await _rows(container) == []


async def test_writing_outside_a_transaction_is_a_programming_error(container: Container) -> None:
    with pytest.raises(RuntimeError, match="No active transaction"):
        await OutboxEventPublisher(container.database).publish([_event()])


async def test_relay_delivers_committed_events_once(container: Container) -> None:
    received: list[EventMessage] = []

    async def handler(message: EventMessage) -> None:
        received.append(message)

    bus = EventBus()
    bus.subscribe("tests.something.happened", handler)
    relay = OutboxRelay(container.database, bus)
    event = _event("hello")
    await _publish(container, event)

    assert await relay.relay_batch() == 1
    assert await relay.relay_batch() == 0
    assert [(m.id, m.payload["label"]) for m in received] == [(event.event_id, "hello")]
    [row] = await _rows(container)
    assert row["published_at"] is not None


async def test_rolled_back_events_never_reach_subscribers(container: Container) -> None:
    received: list[EventMessage] = []

    async def handler(message: EventMessage) -> None:
        received.append(message)

    bus = EventBus()
    bus.subscribe("tests.something.happened", handler)
    await _publish(container, _event(), fail=True)

    assert await OutboxRelay(container.database, bus).relay_batch() == 0
    assert received == []


async def test_a_failing_subscriber_is_retried_later(container: Container) -> None:
    calls = 0

    async def flaky(_: EventMessage) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("smtp down")

    bus = EventBus()
    bus.subscribe("tests.something.happened", flaky)
    relay = OutboxRelay(container.database, bus)
    await _publish(container, _event())

    assert await relay.relay_batch() == 1
    [row] = await _rows(container)
    assert row["published_at"] is None
    assert row["attempts"] == 1
    assert "smtp down" in str(row["last_error"])
    assert row["available_at"] > datetime.now(UTC)  # type: ignore[operator]
    assert await relay.relay_batch() == 0  # backing off: not due yet

    async with container.database.engine.begin() as connection:  # time passes
        await connection.execute(update(outbox).values(available_at=func.now()))
    assert await relay.relay_batch() == 1
    [row] = await _rows(container)
    assert row["published_at"] is not None and calls == 2


async def test_events_without_subscribers_are_marked_published(container: Container) -> None:
    await _publish(container, _event())
    assert await OutboxRelay(container.database, EventBus()).relay_batch() == 1
    [row] = await _rows(container)
    assert row["published_at"] is not None
