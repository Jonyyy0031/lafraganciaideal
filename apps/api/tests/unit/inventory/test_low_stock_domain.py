"""Plan 004, domain: `StockItem.track_low` (the alert state) and the `StockWentLow` event."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.modules.inventory.domain.stock import (
    MovementKind,
    Reason,
    StockItem,
    StockWentLow,
    Threshold,
    low_stock_event,
)
from fragancia_api.shared.kernel import Ok

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
GLOBAL = Threshold(3)
PRESENTATION = UUID(int=7)


def item(
    on_hand: int,
    *,
    override: int | None = None,
    reserved: int = 0,
    alerted: bool = False,
    presentation_id: UUID = PRESENTATION,
) -> StockItem:
    return StockItem(
        presentation_id=presentation_id,
        on_hand=on_hand,
        reserved=reserved,
        threshold_override=Threshold(override) if override is not None else None,
        updated_at=NOW,
        low_alerted=alerted,
    )


def test_a_new_item_is_not_alerted_and_an_empty_one_neither() -> None:
    assert item(5).low_alerted is False
    assert StockItem.empty(UUID(int=1), NOW).low_alerted is False


# --- boundaries of the global threshold --------------------------------------------------------


@pytest.mark.parametrize(
    ("on_hand", "expected"),
    [(4, False), (3, True), (2, True), (0, True)],
    ids=["above", "equal", "below", "zero"],
)
def test_the_first_time_on_hand_reaches_the_threshold_it_returns_true(
    on_hand: int, expected: bool
) -> None:
    stock = item(on_hand)

    assert stock.track_low(GLOBAL) is expected
    assert stock.low_alerted is expected


def test_while_it_stays_low_it_does_not_alert_again() -> None:
    stock = item(3)
    assert stock.track_low(GLOBAL) is True

    stock.on_hand = 2
    assert stock.track_low(GLOBAL) is False
    stock.on_hand = 0
    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is True


def test_going_above_the_threshold_re_arms_it_silently() -> None:
    stock = item(3, alerted=True)

    stock.on_hand = 4
    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is False

    stock.on_hand = 3
    assert stock.track_low(GLOBAL) is True


def test_an_item_above_the_threshold_that_was_never_alerted_stays_quiet() -> None:
    stock = item(10)

    assert stock.track_low(GLOBAL) is False
    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is False


# --- override versus global --------------------------------------------------------------------


def test_the_override_wins_over_the_global_threshold_when_it_is_higher() -> None:
    stock = item(6, override=10)

    assert stock.track_low(GLOBAL) is True


def test_the_override_wins_over_the_global_threshold_when_it_is_lower() -> None:
    stock = item(2, override=1)

    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is False


def test_an_override_of_zero_alerts_only_at_zero_units() -> None:
    stock = item(1, override=0)
    assert stock.track_low(GLOBAL) is False

    stock.on_hand = 0
    assert stock.track_low(GLOBAL) is True


def test_a_new_global_threshold_is_what_track_low_compares_against() -> None:
    stock = item(5)
    assert stock.track_low(GLOBAL) is False

    assert stock.track_low(Threshold(5)) is True
    assert stock.track_low(Threshold(2)) is False
    assert stock.low_alerted is False


def test_removing_an_override_re_arms_an_item_that_is_now_above_the_global_one() -> None:
    stock = item(6, override=10, alerted=True)

    stock.set_threshold(None)

    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is False


# --- units on hand, never available ------------------------------------------------------------


def test_reserved_units_do_not_make_an_item_low() -> None:
    stock = item(4, reserved=3)  # available 1, which the panel calls low

    assert stock.available == 1
    assert stock.is_low(GLOBAL) is True
    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is False


def test_releasing_reserved_units_cannot_re_arm_an_alerted_item() -> None:
    stock = item(3, reserved=2, alerted=True)

    stock.unhold(2)

    assert stock.track_low(GLOBAL) is False
    assert stock.low_alerted is True


def test_holding_units_of_an_item_above_the_threshold_never_alerts() -> None:
    stock = item(4)
    assert isinstance(stock.hold(4), Ok)

    assert stock.available == 0
    assert stock.track_low(GLOBAL) is False


def test_a_sale_that_leaves_on_hand_at_the_threshold_alerts() -> None:
    stock = item(5, reserved=2)

    sold = stock.sell(2, Reason("Venta"), "actor", NOW, held=True)

    assert isinstance(sold, Ok)
    assert stock.on_hand == 3
    assert stock.track_low(GLOBAL) is True


def test_a_movement_that_takes_on_hand_to_the_threshold_alerts() -> None:
    stock = item(5)

    stock.record_movement(MovementKind.LOSS, 2, Reason("Rotura"), "actor", NOW)

    assert stock.track_low(GLOBAL) is True


# --- the event ---------------------------------------------------------------------------------


def test_the_event_is_named_for_other_modules_to_subscribe_by_string() -> None:
    assert StockWentLow.name == "inventory.stock.went_low"


def test_the_event_lists_each_item_with_its_units_and_effective_threshold() -> None:
    first = item(3, presentation_id=UUID(int=1))
    second = item(1, override=8, presentation_id=UUID(int=2))

    event = low_stock_event([first, second], GLOBAL)

    assert event is not None
    assert event.lines == (
        {"presentation_id": str(UUID(int=1)), "on_hand": 3, "threshold": 3},
        {"presentation_id": str(UUID(int=2)), "on_hand": 1, "threshold": 8},
    )


def test_the_payload_turns_the_lines_into_json_values() -> None:
    event = low_stock_event([item(2, presentation_id=UUID(int=5))], GLOBAL)

    assert event is not None
    assert event.payload() == {
        "lines": [{"presentation_id": str(UUID(int=5)), "on_hand": 2, "threshold": 3}]
    }


def test_no_items_means_no_event() -> None:
    assert low_stock_event([], GLOBAL) is None


def test_the_event_uses_on_hand_even_when_units_are_reserved() -> None:
    event = low_stock_event([item(3, reserved=3)], GLOBAL)

    assert event is not None
    assert event.lines[0]["on_hand"] == 3
