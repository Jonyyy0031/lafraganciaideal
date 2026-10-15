"""Plan platform-api-foundation/004 against `fragancia_test`: the relay's retry cap, failed rows
being skipped, and `SqlOutboxAdmin` (list order/total/paging, retry). The autouse fixture in
`conftest.py` truncates `platform.outbox` before and after every test."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update
from structlog.testing import capture_logs

from fragancia_api.container import Container
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.outbox import (
    MAX_ATTEMPTS,
    EventBus,
    OutboxRelay,
    SqlOutboxAdmin,
    outbox,
)
from fragancia_api.shared.kernel import new_id

pytestmark = pytest.mark.integration

NAME = "tests.something.happened"
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


async def seed(
    container: Container,
    *,
    attempts: int = 0,
    failed_at: datetime | None = None,
    published_at: datetime | None = None,
    available_at: datetime | None = None,
    last_error: str | None = None,
    occurred_at: datetime = T0,
    event_id: UUID | None = None,
) -> UUID:
    identifier = event_id or new_id()
    values: dict[str, object] = {
        "id": identifier,
        "name": NAME,
        "payload": {"label": "x"},
        "occurred_at": occurred_at,
        "attempts": attempts,
        "failed_at": failed_at,
        "published_at": published_at,
        "last_error": last_error,
    }
    if available_at is not None:
        values["available_at"] = available_at
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(outbox).values(**values))
    return identifier


async def make_due(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(
            update(outbox).values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def row(container: Container, event_id: UUID) -> dict[str, object]:
    async with container.database.reader() as session:
        result = await session.execute(select(outbox).where(outbox.c.id == event_id))
        return dict(result.mappings().one())


class Handler:
    def __init__(self, *, fails: bool) -> None:
        self.fails = fails
        self.calls = 0

    async def __call__(self, _: EventMessage) -> None:
        self.calls += 1
        if self.fails:
            raise ConnectionError("smtp down")


def relay_with(container: Container, handler: Handler) -> OutboxRelay:
    bus = EventBus()
    bus.subscribe(NAME, handler)
    return OutboxRelay(container.database, bus)


def test_the_cap_is_295_attempts() -> None:
    assert MAX_ATTEMPTS == 295


async def test_an_event_still_below_the_cap_is_retried_with_backoff(container: Container) -> None:
    event_id = await seed(container, attempts=5)
    handler = Handler(fails=True)
    relay = relay_with(container, handler)

    with capture_logs() as logs:
        assert await relay.relay_batch() == 1

    stored = await row(container, event_id)
    assert stored["attempts"] == 6
    assert stored["failed_at"] is None
    assert stored["available_at"] > datetime.now(UTC)  # type: ignore[operator]
    assert [entry["event"] for entry in logs] == ["outbox.delivery_failed"]
    await make_due(container)
    assert await relay.relay_batch() == 1  # still picked up by the relay


async def test_the_attempt_that_reaches_the_cap_marks_the_event_failed_and_logs_it(
    container: Container,
) -> None:
    event_id = await seed(container, attempts=MAX_ATTEMPTS - 1)
    relay = relay_with(container, Handler(fails=True))
    before = datetime.now(UTC)

    with capture_logs() as logs:
        assert await relay.relay_batch() == 1

    stored = await row(container, event_id)
    assert stored["attempts"] == MAX_ATTEMPTS
    assert stored["published_at"] is None
    assert "smtp down" in str(stored["last_error"])
    assert stored["failed_at"] is not None and stored["failed_at"] >= before  # type: ignore[operator]
    [entry] = logs
    assert entry["event"] == "outbox.delivery_failed_permanently"
    assert entry["log_level"] == "error"
    assert entry["event_id"] == str(event_id)
    assert entry["event_name"] == NAME
    assert entry["attempts"] == MAX_ATTEMPTS
    assert "smtp down" in entry["error"]


async def test_a_failed_event_is_never_picked_again_even_when_due(container: Container) -> None:
    event_id = await seed(container, attempts=MAX_ATTEMPTS - 1)
    handler = Handler(fails=True)
    relay = relay_with(container, handler)
    await relay.relay_batch()
    assert handler.calls == 1

    await make_due(container)

    assert await relay.relay_batch() == 0
    assert handler.calls == 1
    assert (await row(container, event_id))["attempts"] == MAX_ATTEMPTS


async def test_a_failed_row_does_not_block_the_due_rows_after_it(container: Container) -> None:
    failed = await seed(
        container,
        attempts=MAX_ATTEMPTS,
        failed_at=T0,
        occurred_at=T0,
        available_at=T0,
        last_error="boom",
    )
    healthy = await seed(container, occurred_at=T0 + timedelta(minutes=1))
    handler = Handler(fails=False)

    assert await relay_with(container, handler).relay_batch() == 1

    assert handler.calls == 1
    assert (await row(container, healthy))["published_at"] is not None
    skipped = await row(container, failed)
    assert skipped["published_at"] is None and skipped["attempts"] == MAX_ATTEMPTS


# Regressions for plans/findings/platform-outbox-backoff-overflows-before-the-cap.md: the backoff
# used to build timedelta(seconds=2**attempts) before the cap and overflowed from about attempt 47.


async def test_a_failure_recorded_far_below_the_cap_still_backs_off_at_the_maximum(
    container: Container,
) -> None:
    event_id = await seed(container, attempts=60)

    assert await relay_with(container, Handler(fails=True)).relay_batch() == 1

    stored = await row(container, event_id)
    assert stored["attempts"] == 61
    assert stored["failed_at"] is None
    wait = stored["available_at"] - datetime.now(UTC)  # type: ignore[operator]
    assert timedelta(minutes=4) < wait <= timedelta(minutes=5)


async def test_an_always_failing_subscriber_fails_the_event_after_exactly_max_attempts(
    container: Container,
) -> None:
    event_id = await seed(container)
    handler = Handler(fails=True)
    relay = relay_with(container, handler)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        await make_due(container)
        assert await relay.relay_batch() == 1
        stored = await row(container, event_id)
        assert stored["attempts"] == attempt
        assert (stored["failed_at"] is not None) == (attempt == MAX_ATTEMPTS)

    await make_due(container)
    assert await relay.relay_batch() == 0
    assert handler.calls == MAX_ATTEMPTS


async def test_a_transient_failure_still_succeeds_later_and_never_fails(
    container: Container,
) -> None:
    event_id = await seed(container)
    handler = Handler(fails=True)
    relay = relay_with(container, handler)
    await relay.relay_batch()
    handler.fails = False
    await make_due(container)

    assert await relay.relay_batch() == 1

    stored = await row(container, event_id)
    assert stored["published_at"] is not None
    assert stored["failed_at"] is None


async def test_failed_lists_only_failed_rows_newest_failure_first_ties_by_id(
    container: Container,
) -> None:
    older = await seed(container, attempts=295, failed_at=T0, last_error="old")
    newer = await seed(container, attempts=295, failed_at=T0 + timedelta(hours=1), last_error="new")
    tie_high = await seed(
        container, attempts=295, failed_at=T0, event_id=UUID(int=2**100), last_error="tie"
    )
    tie_low = await seed(
        container, attempts=295, failed_at=T0, event_id=UUID(int=1), last_error="tie"
    )
    await seed(container, attempts=3)  # pending
    await seed(container, published_at=T0)  # published

    page = await SqlOutboxAdmin(container.database).failed(page=1, size=20)

    assert page.total == 4
    assert (page.page, page.size) == (1, 20)
    ties = sorted([older, tie_high, tie_low])
    assert [item.id for item in page.items] == [newer, *ties]
    first = page.items[0]
    assert (first.name, first.attempts, first.last_error) == (NAME, 295, "new")
    assert first.failed_at == T0 + timedelta(hours=1)
    assert first.occurred_at == T0


async def test_failed_pages_slice_the_list_and_keep_the_total(container: Container) -> None:
    ids = [
        await seed(container, attempts=295, failed_at=T0 + timedelta(minutes=minute))
        for minute in range(5)
    ]
    newest_first = list(reversed(ids))
    admin = SqlOutboxAdmin(container.database)

    first = await admin.failed(page=1, size=2)
    last = await admin.failed(page=3, size=2)
    beyond = await admin.failed(page=4, size=2)

    assert [item.id for item in first.items] == newest_first[:2]
    assert [item.id for item in last.items] == newest_first[4:]
    assert (first.total, last.total, beyond.total) == (5, 5, 5)
    assert beyond.items == []


async def test_failed_is_empty_when_nothing_failed(container: Container) -> None:
    await seed(container)

    page = await SqlOutboxAdmin(container.database).failed(page=1, size=20)

    assert (page.items, page.total) == ([], 0)


async def test_retry_resets_the_window_and_keeps_the_last_error(container: Container) -> None:
    event_id = await seed(
        container,
        attempts=MAX_ATTEMPTS,
        failed_at=T0,
        available_at=datetime.now(UTC) + timedelta(days=1),
        last_error="ConnectionError('smtp down')",
    )

    assert await SqlOutboxAdmin(container.database).retry(event_id) is True

    stored = await row(container, event_id)
    assert stored["failed_at"] is None
    assert stored["attempts"] == 0
    assert stored["available_at"] <= datetime.now(UTC)  # type: ignore[operator]
    assert stored["last_error"] == "ConnectionError('smtp down')"
    assert stored["published_at"] is None


async def test_retry_only_matches_failed_rows(container: Container) -> None:
    pending = await seed(container, attempts=4, last_error="e")
    published = await seed(container, published_at=T0)
    admin = SqlOutboxAdmin(container.database)

    assert await admin.retry(pending) is False
    assert await admin.retry(published) is False
    assert await admin.retry(new_id()) is False

    assert (await row(container, pending))["attempts"] == 4
    assert (await row(container, published))["published_at"] is not None


async def test_a_second_retry_of_the_same_event_does_not_match(container: Container) -> None:
    event_id = await seed(container, attempts=MAX_ATTEMPTS, failed_at=T0)
    admin = SqlOutboxAdmin(container.database)

    assert await admin.retry(event_id) is True
    assert await admin.retry(event_id) is False


async def test_a_retried_event_is_delivered_by_the_relay_once_the_cause_is_fixed(
    container: Container,
) -> None:
    event_id = await seed(container, attempts=MAX_ATTEMPTS - 1)
    handler = Handler(fails=True)
    relay = relay_with(container, handler)
    await relay.relay_batch()
    assert (await row(container, event_id))["failed_at"] is not None

    assert await SqlOutboxAdmin(container.database).retry(event_id) is True
    handler.fails = False

    assert await relay.relay_batch() == 1
    stored = await row(container, event_id)
    assert stored["published_at"] is not None
    assert stored["failed_at"] is None
    assert handler.calls == 2


async def test_a_retried_event_gets_a_fresh_window_and_fails_again_only_at_the_cap(
    container: Container,
) -> None:
    event_id = await seed(container, attempts=MAX_ATTEMPTS, failed_at=T0)
    await SqlOutboxAdmin(container.database).retry(event_id)
    relay = relay_with(container, Handler(fails=True))

    assert await relay.relay_batch() == 1

    stored = await row(container, event_id)
    assert stored["attempts"] == 1
    assert stored["failed_at"] is None
