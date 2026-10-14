import pytest

from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import Threshold
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.inventory.support import MADE_TO_ORDER, STOCKED, UNKNOWN, Inventory


def req(kind: str, quantity: int, reason: str = "Compra") -> MovementRequest:
    return MovementRequest(kind=kind, quantity=quantity, reason=reason)


async def test_the_first_movement_creates_the_stock_item() -> None:
    inv = Inventory()

    result = await inv.record.execute(STOCKED, req("restock", 10), "actor-1")

    assert isinstance(result, Ok)
    assert result.value.on_hand == 10
    assert inv.store.items[STOCKED].on_hand == 10
    [movement] = inv.store.movements_log
    assert movement.id == result.value.id
    assert (movement.actor_id, movement.created_at) == ("actor-1", inv.clock.now())


async def test_successive_movements_follow_the_ledger() -> None:
    inv = Inventory()

    await inv.record.execute(STOCKED, req("restock", 10), "a")
    await inv.record.execute(STOCKED, req("loss", 3), "a")
    await inv.record.execute(STOCKED, req("return", 1), "a")
    await inv.record.execute(STOCKED, req("adjustment", -2), "a")
    last = await inv.record.execute(STOCKED, req("count", 20), "a")

    assert isinstance(last, Ok)
    assert last.value.on_hand == 20
    assert [m.on_hand_after for m in inv.store.movements_log] == [10, 7, 8, 6, 20]
    assert inv.store.movements_log[-1].delta == 14


async def test_an_unknown_presentation_is_not_found() -> None:
    inv = Inventory()

    result = await inv.record.execute(UNKNOWN, req("restock", 1), "a")

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_FOUND"
    assert inv.store.items == {}


async def test_a_made_to_order_presentation_is_not_stocked() -> None:
    inv = Inventory()

    result = await inv.record.execute(MADE_TO_ORDER, req("restock", 1), "a")

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"
    assert inv.store.items == {}


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201])
async def test_an_invalid_reason_is_refused(reason: str) -> None:
    inv = Inventory()

    result = await inv.record.execute(STOCKED, req("restock", 1, reason), "a")

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_REASON_INVALID"
    assert inv.store.movements_log == []


async def test_an_invalid_quantity_is_refused_and_nothing_is_saved() -> None:
    inv = Inventory()

    result = await inv.record.execute(STOCKED, req("restock", 0), "a")

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_QUANTITY_INVALID"
    assert (inv.store.items, inv.store.movements_log) == ({}, [])


async def test_a_loss_beyond_the_units_on_hand_changes_nothing() -> None:
    inv = Inventory()
    await inv.record.execute(STOCKED, req("restock", 10), "a")

    result = await inv.record.execute(STOCKED, req("loss", 50), "a")

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_STOCK_INSUFFICIENT"
    assert inv.store.items[STOCKED].on_hand == 10
    assert len(inv.store.movements_log) == 1


async def test_set_threshold_creates_the_item_at_zero_and_stores_the_override() -> None:
    inv = Inventory()

    result = await inv.set_threshold.execute(STOCKED, 25)

    assert isinstance(result, Ok)
    item = inv.store.items[STOCKED]
    assert (item.on_hand, item.threshold_override) == (0, Threshold(25))
    assert inv.store.movements_log == []


async def test_set_threshold_null_clears_the_override_and_keeps_the_units() -> None:
    inv = Inventory()
    await inv.record.execute(STOCKED, req("restock", 4), "a")
    await inv.set_threshold.execute(STOCKED, 25)

    result = await inv.set_threshold.execute(STOCKED, None)

    assert isinstance(result, Ok)
    item = inv.store.items[STOCKED]
    assert (item.on_hand, item.threshold_override) == (4, None)


@pytest.mark.parametrize("value", [-1, 1001])
async def test_set_threshold_rejects_out_of_range(value: int) -> None:
    inv = Inventory()

    result = await inv.set_threshold.execute(STOCKED, value)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_THRESHOLD_INVALID"


async def test_set_threshold_refuses_unknown_and_made_to_order() -> None:
    inv = Inventory()

    unknown = await inv.set_threshold.execute(UNKNOWN, 5)
    made_to_order = await inv.set_threshold.execute(MADE_TO_ORDER, 5)

    assert isinstance(unknown, Err)
    assert unknown.error.code == "INVENTORY_PRESENTATION_NOT_FOUND"
    assert isinstance(made_to_order, Err)
    assert made_to_order.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"
    assert inv.store.items == {}


async def test_update_settings_changes_the_global_threshold() -> None:
    inv = Inventory()

    result = await inv.update_settings.execute(5)

    assert isinstance(result, Ok)
    assert inv.store.settings.low_stock_threshold == Threshold(5)


@pytest.mark.parametrize("value", [-1, 1001])
async def test_update_settings_rejects_out_of_range(value: int) -> None:
    inv = Inventory()

    result = await inv.update_settings.execute(value)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_THRESHOLD_INVALID"
    assert inv.store.settings.low_stock_threshold == Threshold(3)
