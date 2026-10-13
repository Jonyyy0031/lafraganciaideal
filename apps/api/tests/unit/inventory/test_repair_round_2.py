"""Plan 001, repair round 2: timestamps are read again once the stock lock is held (L4)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import StockItem
from fragancia_api.modules.inventory.infrastructure.in_memory import (
    InMemorySettings,
    InMemoryStock,
)
from fragancia_api.shared.infrastructure.in_memory import InMemoryTransactionRunner
from fragancia_api.shared.kernel import Ok
from tests.unit.inventory.support import STOCKED, make_catalog

START = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)


class AdvancingClock:
    """Every call returns a later instant: START, START + 1 min, ..."""

    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        value = START + timedelta(minutes=self.calls)
        self.calls += 1
        return value


class SpyStock(InMemoryStock):
    def __init__(self) -> None:
        super().__init__()
        self.lock_times: list[datetime] = []

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        self.lock_times.append(now)
        return await super().get_for_update(presentation_id, now=now)


async def test_the_movement_time_is_read_after_the_stock_lock() -> None:
    stock, clock = SpyStock(), AdvancingClock()
    record = RecordMovement(
        stock=stock,
        settings=InMemorySettings(stock),
        catalog=make_catalog(),
        transactions=InMemoryTransactionRunner(),
        clock=clock,
    )

    result = await record.execute(
        STOCKED, MovementRequest(kind="restock", quantity=3, reason="x"), "a"
    )

    assert isinstance(result, Ok)
    [movement] = stock.movements_log
    assert stock.lock_times == [START]  # the first read feeds only the lock's placeholder
    assert movement.created_at == START + timedelta(minutes=1)  # the read after the lock
    assert stock.items[STOCKED].updated_at == movement.created_at
    assert clock.calls == 2


async def test_a_second_movement_on_an_existing_item_also_uses_the_post_lock_time() -> None:
    stock, clock = SpyStock(), AdvancingClock()
    record = RecordMovement(
        stock=stock,
        settings=InMemorySettings(stock),
        catalog=make_catalog(),
        transactions=InMemoryTransactionRunner(),
        clock=clock,
    )
    request = MovementRequest(kind="restock", quantity=1, reason="x")

    await record.execute(STOCKED, request, "a")
    await record.execute(STOCKED, request, "a")

    first, second = stock.movements_log
    assert stock.lock_times == [START, START + timedelta(minutes=2)]
    assert (first.created_at, second.created_at) == (
        START + timedelta(minutes=1),
        START + timedelta(minutes=3),
    )


async def test_the_threshold_updated_at_is_read_after_the_stock_lock() -> None:
    stock, clock = SpyStock(), AdvancingClock()
    set_threshold = SetThreshold(
        stock=stock,
        catalog=make_catalog(),
        transactions=InMemoryTransactionRunner(),
        clock=clock,
    )

    result = await set_threshold.execute(STOCKED, 5)

    assert isinstance(result, Ok)
    assert stock.lock_times == [START]
    assert stock.items[STOCKED].updated_at == START + timedelta(minutes=1)
