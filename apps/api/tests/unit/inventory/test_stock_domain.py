from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.modules.inventory.domain.errors import StockInsufficient, StockQuantityInvalid
from fragancia_api.modules.inventory.domain.stock import (
    QUANTITY_MAX,
    InventorySettings,
    Movement,
    MovementKind,
    Reason,
    StockItem,
    Threshold,
)
from fragancia_api.shared.kernel import Err, Ok, Result

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
PRESENTATION = UUID(int=7)
REASON = Reason("Compra")


def item(on_hand: int = 0, override: int | None = None) -> StockItem:
    return StockItem(
        presentation_id=PRESENTATION,
        on_hand=on_hand,
        threshold_override=Threshold(override) if override is not None else None,
        updated_at=NOW,
    )


def record(
    stock: StockItem, kind: MovementKind, quantity: int
) -> Result[Movement, StockQuantityInvalid | StockInsufficient]:
    return stock.record_movement(kind, quantity, REASON, "actor-1", NOW)


# --- Reason / Threshold ------------------------------------------------------------------------


def test_reason_trims_and_collapses_inner_whitespace() -> None:
    result = Reason.create("  Compra   al \n proveedor ")

    assert result == Ok(Reason("Compra al proveedor"))


@pytest.mark.parametrize("raw", ["", "   ", "x" * 201])
def test_reason_rejects_empty_or_too_long(raw: str) -> None:
    result = Reason.create(raw)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_REASON_INVALID"


@pytest.mark.parametrize("raw", ["x", "x" * 200])
def test_reason_accepts_the_boundaries(raw: str) -> None:
    assert isinstance(Reason.create(raw), Ok)


@pytest.mark.parametrize("value", [0, 1000])
def test_threshold_accepts_the_boundaries(value: int) -> None:
    assert Threshold.create(value) == Ok(Threshold(value))


@pytest.mark.parametrize("value", [-1, 1001])
def test_threshold_rejects_out_of_range(value: int) -> None:
    result = Threshold.create(value)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_THRESHOLD_INVALID"


def test_default_global_threshold_is_three() -> None:
    assert InventorySettings().low_stock_threshold == Threshold(3)


# --- StockItem.record_movement -----------------------------------------------------------------


def test_a_new_item_starts_at_zero_without_movements() -> None:
    stock = StockItem.empty(PRESENTATION, NOW)

    assert (stock.on_hand, stock.threshold_override, stock.new_movements) == (0, None, [])


@pytest.mark.parametrize(
    ("kind", "start", "quantity", "delta", "after"),
    [
        (MovementKind.RESTOCK, 0, 10, 10, 10),
        (MovementKind.RETURN, 7, 1, 1, 8),
        (MovementKind.LOSS, 10, 3, -3, 7),
        (MovementKind.ADJUSTMENT, 8, -2, -2, 6),
        (MovementKind.ADJUSTMENT, 6, 4, 4, 10),
        (MovementKind.COUNT, 6, 20, 14, 20),
        (MovementKind.COUNT, 6, 2, -4, 2),
    ],
)
def test_each_kind_changes_on_hand_as_the_ledger_table_says(
    kind: MovementKind, start: int, quantity: int, delta: int, after: int
) -> None:
    stock = item(start)

    result = record(stock, kind, quantity)

    assert isinstance(result, Ok)
    movement = result.value
    assert (movement.delta, movement.on_hand_after, movement.quantity) == (delta, after, quantity)
    assert (movement.kind, movement.actor_id, movement.reason, movement.created_at) == (
        kind,
        "actor-1",
        REASON,
        NOW,
    )
    assert movement.presentation_id == PRESENTATION
    assert stock.on_hand == after
    assert stock.new_movements == [movement]


def test_a_count_of_zero_is_allowed_and_empties_the_stock() -> None:
    stock = item(5)

    result = record(stock, MovementKind.COUNT, 0)

    assert isinstance(result, Ok)
    assert (result.value.delta, stock.on_hand) == (-5, 0)


def test_a_loss_down_to_exactly_zero_is_allowed() -> None:
    stock = item(3)

    assert isinstance(record(stock, MovementKind.LOSS, 3), Ok)
    assert stock.on_hand == 0


@pytest.mark.parametrize(
    ("kind", "quantity"),
    [(MovementKind.LOSS, 4), (MovementKind.ADJUSTMENT, -4)],
)
def test_a_movement_that_would_go_below_zero_is_refused_and_changes_nothing(
    kind: MovementKind, quantity: int
) -> None:
    stock = item(3)

    result = record(stock, kind, quantity)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_STOCK_INSUFFICIENT"
    assert result.error.details == {"on_hand": 3}
    assert (stock.on_hand, stock.new_movements) == (3, [])


@pytest.mark.parametrize(
    ("kind", "quantity"),
    [
        (MovementKind.RESTOCK, 0),
        (MovementKind.RESTOCK, -1),
        (MovementKind.RESTOCK, QUANTITY_MAX + 1),
        (MovementKind.RETURN, 0),
        (MovementKind.LOSS, 0),
        (MovementKind.LOSS, QUANTITY_MAX + 1),
        (MovementKind.ADJUSTMENT, 0),
        (MovementKind.ADJUSTMENT, QUANTITY_MAX + 1),
        (MovementKind.ADJUSTMENT, -QUANTITY_MAX - 1),
        (MovementKind.COUNT, -1),
        (MovementKind.COUNT, QUANTITY_MAX + 1),
    ],
)
def test_a_quantity_outside_its_range_is_refused(kind: MovementKind, quantity: int) -> None:
    stock = item(5)

    result = record(stock, kind, quantity)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_QUANTITY_INVALID"
    assert (stock.on_hand, stock.new_movements) == (5, [])


@pytest.mark.parametrize(
    ("kind", "quantity"),
    [
        (MovementKind.RESTOCK, QUANTITY_MAX),
        (MovementKind.RETURN, 1),
        (MovementKind.ADJUSTMENT, QUANTITY_MAX),
        (MovementKind.COUNT, QUANTITY_MAX),
    ],
)
def test_quantities_at_the_boundaries_are_accepted(kind: MovementKind, quantity: int) -> None:
    assert isinstance(record(item(0), kind, quantity), Ok)


def test_an_adjustment_of_minus_max_is_accepted_when_enough_is_on_hand() -> None:
    assert isinstance(record(item(QUANTITY_MAX), MovementKind.ADJUSTMENT, -QUANTITY_MAX), Ok)


def test_movements_accumulate_in_order() -> None:
    stock = item(0)

    record(stock, MovementKind.RESTOCK, 5)
    record(stock, MovementKind.LOSS, 2)

    assert [m.on_hand_after for m in stock.new_movements] == [5, 3]


# --- thresholds --------------------------------------------------------------------------------


def test_effective_threshold_is_the_override_else_the_global() -> None:
    assert item().effective_threshold(Threshold(3)) == 3
    assert item(override=25).effective_threshold(Threshold(3)) == 25
    assert item(override=0).effective_threshold(Threshold(3)) == 0


def test_is_low_at_equality_and_below_but_not_above() -> None:
    assert item(3).is_low(Threshold(3))
    assert item(2).is_low(Threshold(3))
    assert not item(4).is_low(Threshold(3))


def test_an_override_replaces_the_global_value_in_is_low() -> None:
    assert item(20, override=25).is_low(Threshold(3))
    assert not item(20, override=19).is_low(Threshold(30))


def test_set_threshold_sets_and_clears_the_override() -> None:
    stock = item()

    stock.set_threshold(Threshold(9))
    assert stock.threshold_override == Threshold(9)
    stock.set_threshold(None)
    assert stock.threshold_override is None
