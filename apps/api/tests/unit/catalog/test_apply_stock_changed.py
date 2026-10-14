"""Plan 002: the catalog subscriber of `inventory.stock.changed` (application layer)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.application.handlers.stock_changed import (
    STOCK_CHANGED_EVENT,
    ApplyStockChanged,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import InMemoryTransactionRunner
from fragancia_api.shared.kernel import new_id

PRESENTATION = UUID(int=42)
AS_OF = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)


class RecordingCopy:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int, int, datetime]] = []

    async def apply(
        self, presentation_id: UUID, available: int, threshold: int, as_of: datetime
    ) -> None:
        self.calls.append((presentation_id, available, threshold, as_of))


class CountingTransactions(InMemoryTransactionRunner):
    def __init__(self) -> None:
        self.runs = 0

    async def run[T, E](self, work: Any) -> Any:
        self.runs += 1
        return await work()


def message(**payload: object) -> EventMessage:
    body: dict[str, object] = {
        "presentation_id": str(PRESENTATION),
        "available": 10,
        "threshold": 3,
        "as_of": "2026-10-05T09:30:00+00:00",
    }
    body.update(payload)
    return EventMessage(id=new_id(), name=STOCK_CHANGED_EVENT, payload=body, occurred_at=AS_OF)


def handler() -> tuple[ApplyStockChanged, RecordingCopy, CountingTransactions]:
    copy, transactions = RecordingCopy(), CountingTransactions()
    return ApplyStockChanged(copy=copy, transactions=transactions), copy, transactions


def test_the_catalog_subscribes_by_the_name_inventory_publishes() -> None:
    assert STOCK_CHANGED_EVENT == "inventory.stock.changed"


async def test_it_parses_the_payload_and_applies_it_inside_a_transaction() -> None:
    apply, copy, transactions = handler()

    await apply(message())

    assert copy.calls == [(PRESENTATION, 10, 3, AS_OF)]
    assert transactions.runs == 1


async def test_it_parses_the_as_of_with_any_offset_as_the_same_instant() -> None:
    apply, copy, _ = handler()

    await apply(message(as_of="2026-10-05T03:30:00-06:00"))

    assert copy.calls[0][3] == AS_OF


@pytest.mark.parametrize(
    "bad",
    [
        {"presentation_id": "not-a-uuid"},
        {"available": "many"},
        {"threshold": None},
        {"as_of": "yesterday"},
        {"as_of": "2026-10-05T09:30:00"},  # no time zone: the "newest wins" comparison needs one
    ],
)
async def test_a_malformed_payload_raises_so_the_relay_retries(bad: dict[str, object]) -> None:
    apply, copy, _ = handler()

    with pytest.raises((ValueError, TypeError)):
        await apply(message(**bad))

    assert copy.calls == []


async def test_a_payload_with_a_missing_field_raises() -> None:
    apply, copy, _ = handler()
    incomplete = message()
    del incomplete.payload["available"]

    with pytest.raises(KeyError):
        await apply(incomplete)

    assert copy.calls == []
