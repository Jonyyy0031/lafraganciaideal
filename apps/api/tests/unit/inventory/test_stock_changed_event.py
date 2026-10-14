"""Plan 002: the `inventory.stock.changed` event, built by the aggregate and published by the
three commands (the domain and application layers)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import StockChanged, StockItem, Threshold
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.inventory.support import STOCKED, STOCKED_2, UNKNOWN, Inventory

AS_OF = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)
PRESENTATION = UUID(int=7)


def item(on_hand: int, override: int | None = None) -> StockItem:
    return StockItem(
        presentation_id=PRESENTATION,
        on_hand=on_hand,
        threshold_override=Threshold(override) if override is not None else None,
        updated_at=AS_OF,
    )


def restock(quantity: int) -> MovementRequest:
    return MovementRequest(kind="restock", quantity=quantity, reason="Compra")


def stock_events(inv: Inventory) -> list[StockChanged]:
    events = [e for e in inv.events.published if isinstance(e, StockChanged)]
    assert len(events) == len(inv.events.published)
    return events


# --- domain ------------------------------------------------------------------------------------


def test_the_event_is_named_for_other_modules_to_subscribe_by_string() -> None:
    assert StockChanged.name == "inventory.stock.changed"


def test_the_payload_carries_the_four_fields_as_json_values() -> None:
    event = StockChanged(presentation_id=PRESENTATION, available=10, threshold=3, as_of=AS_OF)

    assert event.payload() == {
        "presentation_id": str(PRESENTATION),
        "available": 10,
        "threshold": 3,
        "as_of": "2026-10-05T09:30:00+00:00",
    }


def test_changed_event_uses_on_hand_and_the_global_threshold_without_an_override() -> None:
    event = item(10).changed_event(Threshold(3), AS_OF)

    assert (event.presentation_id, event.available, event.threshold, event.as_of) == (
        PRESENTATION,
        10,
        3,
        AS_OF,
    )


def test_changed_event_uses_the_override_as_the_effective_threshold() -> None:
    event = item(10, override=15).changed_event(Threshold(3), AS_OF)

    assert (event.available, event.threshold) == (10, 15)


# --- application -------------------------------------------------------------------------------


async def test_a_movement_publishes_one_event_with_the_new_on_hand_and_global_threshold() -> None:
    inv = Inventory()

    await inv.record.execute(STOCKED, restock(10), "actor-1")

    [event] = stock_events(inv)
    assert (event.presentation_id, event.available, event.threshold) == (STOCKED, 10, 3)
    assert event.as_of == inv.clock.now()


async def test_a_movement_after_a_settings_change_carries_the_new_global_threshold() -> None:
    inv = Inventory()
    await inv.update_settings.execute(12)
    inv.events.published.clear()

    await inv.record.execute(STOCKED, restock(10), "actor-1")

    [event] = stock_events(inv)
    assert event.threshold == 12


async def test_a_movement_carries_the_override_of_that_item() -> None:
    inv = Inventory()
    await inv.set_threshold.execute(STOCKED, 15)
    inv.events.published.clear()

    await inv.record.execute(STOCKED, restock(10), "actor-1")

    [event] = stock_events(inv)
    assert (event.available, event.threshold) == (10, 15)


async def test_a_refused_movement_publishes_nothing() -> None:
    inv = Inventory()
    loss = MovementRequest(kind="loss", quantity=1, reason="Rotura")

    refused = await inv.record.execute(STOCKED, loss, "a")
    unknown = await inv.record.execute(UNKNOWN, restock(1), "a")

    assert isinstance(refused, Err) and isinstance(unknown, Err)
    assert inv.events.published == []


async def test_set_threshold_publishes_the_effective_threshold_of_an_uncounted_item() -> None:
    inv = Inventory()

    result = await inv.set_threshold.execute(STOCKED, 15)

    assert isinstance(result, Ok)
    [event] = stock_events(inv)
    assert (event.presentation_id, event.available, event.threshold) == (STOCKED, 0, 15)
    assert event.as_of == inv.clock.now()


async def test_clearing_the_override_publishes_the_global_threshold_again() -> None:
    inv = Inventory()
    await inv.record.execute(STOCKED, restock(10), "a")
    await inv.set_threshold.execute(STOCKED, 15)
    inv.events.published.clear()

    await inv.set_threshold.execute(STOCKED, None)

    [event] = stock_events(inv)
    assert (event.available, event.threshold) == (10, 3)


async def test_a_refused_threshold_publishes_nothing() -> None:
    inv = Inventory()

    result = await inv.set_threshold.execute(UNKNOWN, 5)

    assert isinstance(result, Err)
    assert inv.events.published == []


async def test_a_settings_change_publishes_one_event_per_stock_item() -> None:
    inv = Inventory()
    await inv.record.execute(STOCKED, restock(10), "a")
    await inv.record.execute(STOCKED_2, restock(4), "a")
    await inv.set_threshold.execute(STOCKED_2, 15)
    inv.clock.current = inv.clock.current + timedelta(hours=1)
    inv.events.published.clear()

    result = await inv.update_settings.execute(12)

    assert isinstance(result, Ok)
    events = {e.presentation_id: e for e in stock_events(inv)}
    assert set(events) == {STOCKED, STOCKED_2}
    assert (events[STOCKED].available, events[STOCKED].threshold) == (10, 12)
    # An item with an override keeps it: the new global threshold does not change its status.
    assert (events[STOCKED_2].available, events[STOCKED_2].threshold) == (4, 15)
    assert {e.as_of for e in events.values()} == {inv.clock.now()}


async def test_a_settings_change_with_no_stock_items_publishes_nothing() -> None:
    inv = Inventory()

    result = await inv.update_settings.execute(12)

    assert isinstance(result, Ok)
    assert inv.events.published == []


async def test_an_invalid_global_threshold_publishes_nothing() -> None:
    inv = Inventory()
    await inv.record.execute(STOCKED, restock(10), "a")
    inv.events.published.clear()

    result = await inv.update_settings.execute(1_000_000)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_THRESHOLD_INVALID"
    assert inv.events.published == []
