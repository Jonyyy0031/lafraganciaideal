"""Plan 002, repair round 1: the lock-taking repository calls and the order the commands use
them in (application layer; the SQL locks themselves are integration tests)."""

from datetime import UTC, datetime
from uuid import UUID

from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import (
    InventorySettings,
    StockItem,
    Threshold,
)
from fragancia_api.modules.inventory.infrastructure.in_memory import (
    InMemorySettings,
    InMemoryStock,
)
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from tests.unit.inventory.support import STOCKED, make_catalog

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class Calls(list[str]):
    """Repository calls in the order the commands made them."""


class LoggedSettings(InMemorySettings):
    def __init__(self, store: InMemoryStock, calls: Calls) -> None:
        super().__init__(store)
        self._calls = calls

    async def get(self) -> InventorySettings:
        self._calls.append("settings.get")
        return await super().get()

    async def get_for_update(self) -> InventorySettings:
        self._calls.append("settings.get_for_update")
        return await super().get_for_update()

    async def get_for_share(self) -> InventorySettings:
        self._calls.append("settings.get_for_share")
        return await super().get_for_share()


class LoggedStock(InMemoryStock):
    def __init__(self, calls: Calls) -> None:
        super().__init__()
        self._calls = calls

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        self._calls.append("stock.get_for_update")
        return await super().get_for_update(presentation_id, now=now)

    async def list_all_for_update(self) -> list[StockItem]:
        self._calls.append("stock.list_all_for_update")
        return await super().list_all_for_update()


class Logged:
    def __init__(self) -> None:
        self.calls = Calls()
        self.store = LoggedStock(self.calls)
        settings = LoggedSettings(self.store, self.calls)
        transactions = InMemoryTransactionRunner()
        events = RecordingEventPublisher()
        clock = FixedClock()
        self.record = RecordMovement(
            stock=self.store,
            settings=settings,
            catalog=make_catalog(),
            transactions=transactions,
            events=events,
            clock=clock,
        )
        self.set_threshold = SetThreshold(
            stock=self.store,
            settings=settings,
            catalog=make_catalog(),
            transactions=transactions,
            events=events,
            clock=clock,
        )
        self.update_settings = UpdateInventorySettings(
            settings=settings,
            stock=self.store,
            transactions=transactions,
            events=events,
            clock=clock,
        )


def item(presentation_id: int, on_hand: int = 1) -> StockItem:
    return StockItem(
        presentation_id=UUID(int=presentation_id),
        on_hand=on_hand,
        threshold_override=None,
        updated_at=NOW,
    )


# --- the in-memory fakes honor the contract ----------------------------------------------------


async def test_the_settings_locks_return_the_current_value() -> None:
    store = InMemoryStock()
    settings = InMemorySettings(store)

    assert await settings.get_for_update() == InventorySettings()  # the default threshold
    assert await settings.get_for_share() == InventorySettings()
    await settings.save(InventorySettings(low_stock_threshold=Threshold(9)))
    assert await settings.get_for_update() == InventorySettings(Threshold(9))
    assert await settings.get_for_share() == InventorySettings(Threshold(9))


async def test_list_all_for_update_is_ordered_by_presentation_id_and_returns_copies() -> None:
    store = InMemoryStock()
    for number in (30, 10, 20):
        store.items[UUID(int=number)] = item(number)

    listed = await store.list_all_for_update()
    listed[0].on_hand = 99

    assert [i.presentation_id for i in listed] == [UUID(int=10), UUID(int=20), UUID(int=30)]
    assert store.items[UUID(int=10)].on_hand == 1


async def test_list_all_for_update_of_an_empty_store_is_empty() -> None:
    assert await InMemoryStock().list_all_for_update() == []


# --- the commands take the locks in the documented order --------------------------------------


async def test_a_movement_takes_the_settings_share_lock_before_the_stock_lock() -> None:
    logged = Logged()

    await logged.record.execute(
        STOCKED, MovementRequest(kind="restock", quantity=2, reason="Compra"), "a"
    )

    assert logged.calls == ["settings.get_for_share", "stock.get_for_update"]


async def test_set_threshold_takes_the_settings_share_lock_before_the_stock_lock() -> None:
    logged = Logged()

    await logged.set_threshold.execute(STOCKED, 5)

    assert logged.calls == ["settings.get_for_share", "stock.get_for_update"]


async def test_a_settings_change_locks_the_settings_row_then_every_stock_item() -> None:
    logged = Logged()

    await logged.update_settings.execute(12)

    assert logged.calls == ["settings.get_for_update", "stock.list_all_for_update"]


async def test_a_refused_movement_takes_no_inventory_lock() -> None:
    logged = Logged()
    unknown = UUID(int=999)

    await logged.record.execute(
        unknown, MovementRequest(kind="restock", quantity=2, reason="Compra"), "a"
    )

    assert logged.calls == []
