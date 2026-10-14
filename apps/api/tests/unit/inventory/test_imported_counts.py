"""Plan 005: `RecordImportedCounts` and its outbox subscriber `ApplyImportedCounts` (application
layer, in-memory adapters), plus the two constants that tie inventory to the catalog import."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import structlog.testing

from fragancia_api.modules.catalog.domain.catalog_import import CatalogImportApplied
from fragancia_api.modules.catalog.domain.import_cells import STOCK_COUNT_MAX
from fragancia_api.modules.inventory.application.commands.stock import IMPORT_COUNT_REASON
from fragancia_api.modules.inventory.application.handlers.import_applied import (
    IMPORT_APPLIED_EVENT,
    ApplyImportedCounts,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import (
    QUANTITY_MAX,
    MovementKind,
    StockChanged,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.kernel import Ok, new_id
from tests.unit.inventory.support import (
    MADE_TO_ORDER,
    STOCKED,
    STOCKED_2,
    UNKNOWN,
    Inventory,
)

ACTOR = "admin-5"
SKIPPED = "inventory.import_count_skipped"


def _skipped(logs: Sequence[Mapping[str, Any]]) -> list[tuple[object, object]]:
    return [(log["presentation_id"], log["code"]) for log in logs if log["event"] == SKIPPED]


async def _seed(inv: Inventory, presentation_id: UUID, on_hand: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=on_hand, reason="Compra"), "a"
    )
    assert isinstance(result, Ok)
    inv.events.published.clear()
    inv.store.movements_log.clear()


# --- the ties between the two modules ----------------------------------------------------------


def test_the_catalog_limit_mirrors_the_inventory_limit() -> None:
    assert STOCK_COUNT_MAX == QUANTITY_MAX


def test_inventory_subscribes_by_the_name_the_catalog_publishes() -> None:
    assert IMPORT_APPLIED_EVENT == CatalogImportApplied.name == "catalog.import.applied"


# --- RecordImportedCounts ----------------------------------------------------------------------


async def test_each_count_sets_on_hand_with_a_count_movement_by_the_actor() -> None:
    inv = Inventory()

    result = await inv.record_counts.execute({STOCKED: 10, STOCKED_2: 0}, ACTOR)

    assert result == Ok(2)
    assert (inv.store.items[STOCKED].on_hand, inv.store.items[STOCKED_2].on_hand) == (10, 0)
    movements = {m.presentation_id: m for m in inv.store.movements_log}
    assert set(movements) == {STOCKED, STOCKED_2}
    for movement in movements.values():
        assert movement.kind is MovementKind.COUNT
        assert (
            movement.reason.value == IMPORT_COUNT_REASON == "Conteo desde la importación de Excel"
        )
        assert (movement.actor_id, movement.created_at) == (ACTOR, inv.clock.now())
    assert (movements[STOCKED].quantity, movements[STOCKED].delta) == (10, 10)
    assert (movements[STOCKED_2].quantity, movements[STOCKED_2].delta) == (0, 0)


async def test_a_count_overwrites_the_existing_stock() -> None:
    inv = Inventory()
    await _seed(inv, STOCKED, 7)

    await inv.record_counts.execute({STOCKED: 4}, ACTOR)

    assert inv.store.items[STOCKED].on_hand == 4
    [movement] = inv.store.movements_log
    assert (movement.delta, movement.on_hand_after) == (-3, 4)


async def test_a_count_equal_to_on_hand_is_still_recorded_with_delta_zero() -> None:
    inv = Inventory()
    await _seed(inv, STOCKED, 4)

    result = await inv.record_counts.execute({STOCKED: 4}, ACTOR)

    assert result == Ok(1)
    [movement] = inv.store.movements_log
    assert (movement.kind, movement.delta, movement.on_hand_after) == (MovementKind.COUNT, 0, 4)
    assert len(inv.events.published) == 1


async def test_counts_are_recorded_in_ascending_presentation_id_order() -> None:
    inv = Inventory()
    descending = {STOCKED_2: 5, STOCKED: 3}  # inserted high first

    await inv.record_counts.execute(descending, ACTOR)

    assert [m.presentation_id for m in inv.store.movements_log] == [STOCKED, STOCKED_2]
    published = [e.presentation_id for e in inv.events.published if isinstance(e, StockChanged)]
    assert published == [STOCKED, STOCKED_2]


async def test_each_count_publishes_one_stock_changed_with_the_new_count_and_the_threshold() -> (
    None
):
    inv = Inventory()

    await inv.record_counts.execute({STOCKED: 10, STOCKED_2: 2}, ACTOR)

    events = inv.events.published
    assert all(isinstance(e, StockChanged) for e in events)
    assert [
        (e.presentation_id, e.available, e.threshold, e.as_of)
        for e in events
        if isinstance(e, StockChanged)
    ] == [
        (STOCKED, 10, 3, inv.clock.now()),
        (STOCKED_2, 2, 3, inv.clock.now()),
    ]


async def test_the_stock_changed_event_uses_the_presentations_own_threshold_override() -> None:
    inv = Inventory()
    assert isinstance(await inv.set_threshold.execute(STOCKED, 8), Ok)
    inv.events.published.clear()

    await inv.record_counts.execute({STOCKED: 10}, ACTOR)

    [event] = inv.events.published
    assert isinstance(event, StockChanged)
    assert (event.available, event.threshold) == (10, 8)


async def test_an_unknown_presentation_is_skipped_with_a_log_and_the_others_are_recorded() -> None:
    inv = Inventory()

    with structlog.testing.capture_logs() as logs:
        result = await inv.record_counts.execute({STOCKED: 5, UNKNOWN: 9}, ACTOR)

    assert result == Ok(1)
    assert set(inv.store.items) == {STOCKED}
    assert _skipped(logs) == [(str(UNKNOWN), "INVENTORY_PRESENTATION_NOT_FOUND")]
    assert len(inv.events.published) == 1


async def test_a_presentation_switched_to_made_to_order_is_skipped_with_a_log() -> None:
    inv = Inventory()

    with structlog.testing.capture_logs() as logs:
        result = await inv.record_counts.execute({MADE_TO_ORDER: 5, STOCKED: 2}, ACTOR)

    assert result == Ok(1)
    assert set(inv.store.items) == {STOCKED}
    assert _skipped(logs) == [(str(MADE_TO_ORDER), "INVENTORY_PRESENTATION_NOT_STOCKED")]
    assert [m.presentation_id for m in inv.store.movements_log] == [STOCKED]


async def test_a_count_the_aggregate_refuses_is_skipped_and_the_others_are_recorded() -> None:
    inv = Inventory()

    with structlog.testing.capture_logs() as logs:
        result = await inv.record_counts.execute({STOCKED: QUANTITY_MAX + 1, STOCKED_2: 5}, ACTOR)

    assert result == Ok(1)
    assert set(inv.store.items) == {STOCKED_2}
    assert _skipped(logs) == [(str(STOCKED), "INVENTORY_QUANTITY_INVALID")]


async def test_when_every_count_is_skipped_nothing_is_written_or_published() -> None:
    inv = Inventory()

    result = await inv.record_counts.execute({UNKNOWN: 1, MADE_TO_ORDER: 1}, ACTOR)

    assert result == Ok(0)
    assert (inv.store.items, inv.store.movements_log, inv.events.published) == ({}, [], [])


async def test_an_empty_map_records_nothing() -> None:
    inv = Inventory()

    result = await inv.record_counts.execute({}, ACTOR)

    assert result == Ok(0)
    assert (inv.store.items, inv.store.movements_log, inv.events.published) == ({}, [], [])


async def test_a_count_of_zero_creates_the_stock_item_for_a_never_counted_presentation() -> None:
    inv = Inventory()

    await inv.record_counts.execute({STOCKED: 0}, ACTOR)

    assert inv.store.items[STOCKED].on_hand == 0
    [movement] = inv.store.movements_log
    assert (movement.kind, movement.quantity, movement.delta) == (MovementKind.COUNT, 0, 0)


# --- ApplyImportedCounts -----------------------------------------------------------------------


def _message(**payload: object) -> EventMessage:
    body: dict[str, object] = {
        "import_id": str(new_id()),
        "actor_id": ACTOR,
        "stock_counts": {str(STOCKED): 10, str(STOCKED_2): 3},
    }
    body.update(payload)
    return EventMessage(
        id=new_id(),
        name=IMPORT_APPLIED_EVENT,
        payload=body,
        occurred_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    )


async def test_the_handler_parses_the_payload_and_records_the_counts_for_the_actor() -> None:
    inv = Inventory()

    await ApplyImportedCounts(inv.record_counts)(_message())

    assert (inv.store.items[STOCKED].on_hand, inv.store.items[STOCKED_2].on_hand) == (10, 3)
    assert {m.actor_id for m in inv.store.movements_log} == {ACTOR}


async def test_the_handler_accepts_the_payload_a_real_event_produces() -> None:
    inv = Inventory()
    event = CatalogImportApplied(import_id=new_id(), actor_id=ACTOR, stock_counts={STOCKED: 6})
    message = EventMessage(
        id=event.event_id,
        name=CatalogImportApplied.name,
        payload=event.payload(),
        occurred_at=event.occurred_at,
    )

    await ApplyImportedCounts(inv.record_counts)(message)

    assert inv.store.items[STOCKED].on_hand == 6


async def test_the_handler_treats_an_empty_map_as_a_no_op() -> None:
    inv = Inventory()

    await ApplyImportedCounts(inv.record_counts)(_message(stock_counts={}))

    assert (inv.store.items, inv.store.movements_log, inv.events.published) == ({}, [], [])


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        ({"stock_counts": {"not-a-uuid": 5}}, ValueError),
        ({"stock_counts": {str(STOCKED): "many"}}, ValueError),
        ({"stock_counts": {str(STOCKED): None}}, ValueError),
        ({"actor_id": 7}, TypeError),
        ({"actor_id": None}, TypeError),
    ],
)
async def test_a_malformed_payload_raises_so_the_relay_retries_and_records_nothing(
    bad: dict[str, object], error: type[Exception]
) -> None:
    inv = Inventory()

    with pytest.raises(error):
        await ApplyImportedCounts(inv.record_counts)(_message(**bad))

    assert (inv.store.items, inv.store.movements_log) == ({}, [])


@pytest.mark.parametrize("missing", ["actor_id", "stock_counts"])
async def test_a_payload_with_a_missing_field_raises(missing: str) -> None:
    inv = Inventory()
    incomplete = _message()
    del incomplete.payload[missing]

    with pytest.raises(KeyError):
        await ApplyImportedCounts(inv.record_counts)(incomplete)

    assert inv.store.movements_log == []


async def test_a_bad_entry_after_a_good_one_records_nothing_because_parsing_comes_first() -> None:
    inv = Inventory()
    payload = {"stock_counts": {str(STOCKED): 10, "oops": 1}}

    with pytest.raises(ValueError):
        await ApplyImportedCounts(inv.record_counts)(_message(**payload))

    assert inv.store.movements_log == []
