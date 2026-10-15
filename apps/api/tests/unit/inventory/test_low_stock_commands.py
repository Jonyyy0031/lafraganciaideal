"""Plan 004, application: the five commands that change units on hand or a threshold publish one
`inventory.stock.went_low` event per execution; the commands that only move `reserved` never do.
In-memory adapters; the fakes keep the alert flag through their deep copies."""

from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import StockChanged, StockWentLow
from fragancia_api.shared.kernel import Ok
from tests.unit.inventory.support import (
    MADE_TO_ORDER,
    STOCKED,
    STOCKED_2,
    UNKNOWN,
    Inventory,
    presentation,
)


def went_low(inv: Inventory) -> list[StockWentLow]:
    return [e for e in inv.events.published if isinstance(e, StockWentLow)]


def line(presentation_id: UUID, on_hand: int, threshold: int = 3) -> dict[str, object]:
    return {"presentation_id": str(presentation_id), "on_hand": on_hand, "threshold": threshold}


async def restock(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="restock", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def lose(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id, MovementRequest(kind="loss", quantity=quantity, reason="x"), "a"
    )
    assert isinstance(result, Ok)


async def reserve(inv: Inventory, quantities: dict[UUID, int], reference: str = "QA-1") -> UUID:
    result = await inv.reserve.execute(
        reference, quantities, inv.clock.now() + timedelta(minutes=2)
    )
    assert isinstance(result, Ok)
    return result.value


def alerted(inv: Inventory, presentation_id: UUID) -> bool:
    return inv.store.items[presentation_id].low_alerted


# --- RecordMovement ----------------------------------------------------------------------------


async def test_a_loss_to_the_threshold_publishes_one_went_low_with_that_line() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    assert went_low(inv) == []

    await lose(inv, STOCKED, 2)

    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 3),)
    assert alerted(inv, STOCKED) is True


async def test_the_went_low_event_follows_the_stock_changed_one_of_the_same_command() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    inv.events.published.clear()

    await lose(inv, STOCKED, 2)

    assert [type(e) for e in inv.events.published] == [StockChanged, StockWentLow]


async def test_a_further_loss_while_low_publishes_nothing_more() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await lose(inv, STOCKED, 2)

    await lose(inv, STOCKED, 1)

    assert len(went_low(inv)) == 1
    assert alerted(inv, STOCKED) is True


async def test_a_restock_above_the_threshold_re_arms_and_the_next_drop_alerts_again() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await lose(inv, STOCKED, 2)

    await restock(inv, STOCKED, 3)  # 6
    assert alerted(inv, STOCKED) is False
    assert len(went_low(inv)) == 1
    await lose(inv, STOCKED, 3)  # 3 again

    assert [e.lines for e in went_low(inv)] == [(line(STOCKED, 3),), (line(STOCKED, 3),)]


async def test_a_restock_that_stays_at_or_below_the_threshold_does_not_re_arm() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 2)  # the first movement creates the item, already low
    assert len(went_low(inv)) == 1

    await restock(inv, STOCKED, 1)  # 3, still low

    assert len(went_low(inv)) == 1
    assert alerted(inv, STOCKED) is True


async def test_a_movement_that_keeps_the_item_above_the_threshold_publishes_nothing() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    await lose(inv, STOCKED, 3)  # 7

    assert went_low(inv) == []
    assert alerted(inv, STOCKED) is False


async def test_a_refused_movement_publishes_nothing_and_leaves_the_flag() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await lose(inv, STOCKED, 2)
    inv.events.published.clear()

    result = await inv.record.execute(
        STOCKED, MovementRequest(kind="loss", quantity=99, reason="x"), "a"
    )

    assert not isinstance(result, Ok)
    assert inv.events.published == []
    assert alerted(inv, STOCKED) is True


async def test_a_movement_uses_the_presentations_override_as_the_threshold() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 20)
    await inv.set_threshold.execute(STOCKED, 10)

    await lose(inv, STOCKED, 10)

    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 10, threshold=10),)


# --- SetThreshold ------------------------------------------------------------------------------


async def test_raising_a_threshold_above_the_units_on_hand_alerts_once() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)

    result = await inv.set_threshold.execute(STOCKED, 10)

    assert isinstance(result, Ok)
    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 6, threshold=10),)
    assert alerted(inv, STOCKED) is True


async def test_lowering_the_threshold_back_re_arms_without_an_event() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)
    await inv.set_threshold.execute(STOCKED, 10)

    await inv.set_threshold.execute(STOCKED, 3)

    assert len(went_low(inv)) == 1
    assert alerted(inv, STOCKED) is False


async def test_clearing_an_override_back_to_the_global_threshold_re_arms_silently() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)
    await inv.set_threshold.execute(STOCKED, 10)

    await inv.set_threshold.execute(STOCKED, None)

    assert len(went_low(inv)) == 1
    assert alerted(inv, STOCKED) is False


async def test_setting_the_same_threshold_again_does_not_alert_again() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)
    await inv.set_threshold.execute(STOCKED, 10)

    await inv.set_threshold.execute(STOCKED, 12)

    assert len(went_low(inv)) == 1


async def test_a_threshold_that_leaves_the_item_above_it_publishes_nothing() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)

    await inv.set_threshold.execute(STOCKED, 5)

    assert went_low(inv) == []


async def test_a_threshold_on_a_never_stocked_presentation_alerts_for_its_empty_item() -> None:
    """Literal to the plan (see its Deviations): the empty item has 0 on hand, which is low."""
    inv = Inventory()

    result = await inv.set_threshold.execute(STOCKED, 5)

    assert isinstance(result, Ok)
    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 0, threshold=5),)


async def test_a_refused_threshold_publishes_nothing() -> None:
    inv = Inventory()

    for presentation_id in (UNKNOWN, MADE_TO_ORDER):
        result = await inv.set_threshold.execute(presentation_id, 5)
        assert not isinstance(result, Ok)

    assert inv.events.published == []


# --- RecordImportedCounts ----------------------------------------------------------------------


async def test_an_import_of_three_counts_at_zero_publishes_one_event_with_three_lines() -> None:
    inv = Inventory()
    third = UUID(int=104)
    inv.catalog.presentations.append(presentation(third, "Versace Eros EDT 30 ml"))

    result = await inv.record_counts.execute({STOCKED_2: 0, third: 0, STOCKED: 0}, "admin")

    assert isinstance(result, Ok)
    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 0), line(STOCKED_2, 0), line(third, 0))
    assert len([e for e in inv.events.published if isinstance(e, StockChanged)]) == 3


async def test_only_the_counts_that_reach_the_threshold_are_listed() -> None:
    inv = Inventory()

    await inv.record_counts.execute({STOCKED: 10, STOCKED_2: 3}, "admin")

    [event] = went_low(inv)
    assert event.lines == (line(STOCKED_2, 3),)


async def test_a_count_that_stays_low_does_not_alert_again() -> None:
    inv = Inventory()
    await inv.record_counts.execute({STOCKED: 0}, "admin")

    await inv.record_counts.execute({STOCKED: 1}, "admin")

    assert len(went_low(inv)) == 1


async def test_counts_all_above_the_threshold_publish_no_went_low() -> None:
    inv = Inventory()

    await inv.record_counts.execute({STOCKED: 10, STOCKED_2: 20}, "admin")

    assert went_low(inv) == []


async def test_an_import_with_nothing_recorded_publishes_no_went_low() -> None:
    inv = Inventory()

    await inv.record_counts.execute({}, "admin")
    await inv.record_counts.execute({UNKNOWN: 0, MADE_TO_ORDER: 0}, "admin")

    assert inv.events.published == []


# --- UpdateInventorySettings -------------------------------------------------------------------


async def test_raising_the_global_threshold_alerts_every_item_it_now_covers_in_one_event() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)
    await restock(inv, STOCKED_2, 8)
    inv.events.published.clear()

    result = await inv.update_settings.execute(10)

    assert isinstance(result, Ok)
    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 6, threshold=10), line(STOCKED_2, 8, threshold=10))
    assert alerted(inv, STOCKED) and alerted(inv, STOCKED_2)


async def test_a_settings_change_leaves_items_with_their_own_override_alone() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)
    await restock(inv, STOCKED_2, 6)
    await inv.set_threshold.execute(STOCKED_2, 2)
    inv.events.published.clear()

    await inv.update_settings.execute(10)

    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 6, threshold=10),)
    assert alerted(inv, STOCKED_2) is False


async def test_lowering_the_global_threshold_re_arms_silently_and_persists_it() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 6)
    await inv.update_settings.execute(10)
    inv.events.published.clear()

    await inv.update_settings.execute(3)

    assert went_low(inv) == []
    assert alerted(inv, STOCKED) is False


async def test_a_settings_change_over_an_already_alerted_item_does_not_repeat_it() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 2)
    inv.events.published.clear()

    await inv.update_settings.execute(5)

    assert went_low(inv) == []


async def test_a_settings_change_with_no_stock_items_publishes_nothing() -> None:
    inv = Inventory()

    await inv.update_settings.execute(7)

    assert inv.events.published == []


# --- CommitReservation -------------------------------------------------------------------------


async def test_a_sale_that_leaves_on_hand_at_the_threshold_alerts() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    assert went_low(inv) == []

    result = await inv.commit.execute(reservation_id)

    assert isinstance(result, Ok)
    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 3),)


async def test_a_commit_that_takes_two_items_low_publishes_one_event() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    await restock(inv, STOCKED_2, 4)
    reservation_id = await reserve(inv, {STOCKED: 2, STOCKED_2: 4})

    await inv.commit.execute(reservation_id)

    [event] = went_low(inv)
    assert event.lines == (line(STOCKED, 3), line(STOCKED_2, 0))


async def test_a_commit_that_stays_above_the_threshold_publishes_nothing() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 10)
    reservation_id = await reserve(inv, {STOCKED: 2})

    await inv.commit.execute(reservation_id)

    assert went_low(inv) == []


async def test_a_commit_of_an_item_that_was_already_alerted_does_not_repeat() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 4)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await lose(inv, STOCKED, 1)  # 3 on hand: alerted
    assert len(went_low(inv)) == 1

    await inv.commit.execute(reservation_id)

    assert len(went_low(inv)) == 1


async def test_committing_twice_is_idempotent_for_the_alert() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2})
    await inv.commit.execute(reservation_id)

    await inv.commit.execute(reservation_id)

    assert len(went_low(inv)) == 1


# --- reserve, extend, release and expire never alert -------------------------------------------


async def test_reserving_so_that_available_is_low_does_not_alert() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 4)
    inv.events.published.clear()

    await reserve(inv, {STOCKED: 1})  # available 3, on hand 4

    assert went_low(inv) == []
    assert alerted(inv, STOCKED) is False


async def test_reserving_and_letting_it_expire_sends_no_alert_and_keeps_the_flag() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 4)
    await reserve(inv, {STOCKED: 1})
    inv.clock.current += timedelta(minutes=3)

    assert await inv.expire.run_once() == 1

    assert went_low(inv) == []
    assert alerted(inv, STOCKED) is False


async def test_releasing_a_reservation_does_not_alert_or_re_arm() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 3)  # created low: alerted
    reservation_id = await reserve(inv, {STOCKED: 1})
    assert len(went_low(inv)) == 1

    await inv.release.execute(reservation_id)

    assert len(went_low(inv)) == 1
    assert alerted(inv, STOCKED) is True


async def test_extending_a_reservation_does_not_alert() -> None:
    inv = Inventory()
    await restock(inv, STOCKED, 4)
    reservation_id = await reserve(inv, {STOCKED: 1})

    await inv.extend.execute(reservation_id, inv.clock.now() + timedelta(minutes=10))

    assert went_low(inv) == []
