"""Plan 001, repair round 1: one low rule (L1), app clock (L2), locked lookup (L3)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
)
from fragancia_api.modules.inventory.application.ports import StockedPresentation
from fragancia_api.modules.inventory.application.queries.stock import GetStock, ListStock
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import (
    StockItem,
    Threshold,
    effective_threshold,
    is_low,
)
from fragancia_api.modules.inventory.infrastructure.in_memory import (
    InMemoryPresentationCatalog,
    InMemorySettings,
    InMemoryStock,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, RecordingEventPublisher
from fragancia_api.shared.kernel import Ok, Result
from tests.unit.inventory.support import STOCKED, Inventory

# --- L1: one rule ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("on_hand", "override", "global_threshold", "low"),
    [
        (3, None, 3, True),  # equality, global
        (4, None, 3, False),
        (2, None, 3, True),
        (5, 5, 3, True),  # equality, override above the global
        (6, 5, 3, False),
        (3, 2, 3, False),  # an override below the global replaces it
        (0, 0, 3, True),  # equality at zero
        (1, 0, 3, False),
    ],
)
async def test_queries_and_domain_agree_on_is_low(
    on_hand: int, override: int | None, global_threshold: int, low: bool
) -> None:
    inv = Inventory()
    await inv.update_settings.execute(global_threshold)
    stock = inv.store
    item = StockItem.empty(STOCKED, inv.clock.now())
    item.on_hand = on_hand
    item.set_threshold(Threshold(override) if override is not None else None)
    await stock.save(item)

    got = await GetStock(inv.catalog, stock).execute(STOCKED)
    listed = await ListStock(inv.catalog, stock).execute(low=False, page=1, size=10)
    line = next(line for line in listed.items if line.presentation_id == STOCKED)

    assert isinstance(got, Ok)
    domain = item.is_low(Threshold(global_threshold))
    assert (domain, is_low(on_hand, override, global_threshold)) == (low, low)
    assert got.value.is_low is low
    assert line.is_low is low
    expected = effective_threshold(override, global_threshold)
    assert got.value.effective_threshold == line.effective_threshold == expected
    assert item.effective_threshold(Threshold(global_threshold)) == expected


def test_the_pure_rule_functions() -> None:
    assert effective_threshold(None, 3) == 3
    assert effective_threshold(0, 3) == 0
    assert effective_threshold(9, 3) == 9
    assert is_low(3, None, 3)
    assert not is_low(4, None, 3)


# --- L2: the clock stamps the placeholder ------------------------------------------------------


class SpyStock(InMemoryStock):
    def __init__(self) -> None:
        super().__init__()
        self.lock_times: list[datetime] = []

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        self.lock_times.append(now)
        return await super().get_for_update(presentation_id, now=now)


async def test_commands_pass_the_injected_clock_to_the_stock_lock() -> None:
    at = datetime(2031, 5, 6, 7, 8, tzinfo=UTC)
    inv = Inventory()
    spy = SpyStock()
    clock = FixedClock(at)
    transactions = inv.record._transactions  # noqa: SLF001  (reuse the same fake runner)
    record = RecordMovement(
        stock=spy,
        settings=InMemorySettings(spy),
        catalog=inv.catalog,
        transactions=transactions,
        events=RecordingEventPublisher(),
        clock=clock,
    )
    threshold = SetThreshold(
        stock=spy,
        settings=InMemorySettings(spy),
        catalog=inv.catalog,
        transactions=transactions,
        events=RecordingEventPublisher(),
        clock=clock,
    )

    await record.execute(STOCKED, MovementRequest(kind="restock", quantity=1, reason="x"), "a")
    await threshold.execute(STOCKED, 5)

    assert spy.lock_times == [at, at]


# --- L3: the catalog lookup is made inside the transaction -------------------------------------


class TrackingTransactions:
    def __init__(self) -> None:
        self.inside = False

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        self.inside = True
        try:
            return await work()
        finally:
            self.inside = False


class TrackingCatalog(InMemoryPresentationCatalog):
    def __init__(self, transactions: TrackingTransactions, *presentations: StockedPresentation):
        super().__init__(*presentations)
        self._transactions = transactions
        self.calls: list[tuple[str, bool]] = []

    async def describe(self, ids):  # type: ignore[no-untyped-def]
        self.calls.append(("describe", self._transactions.inside))
        return await super().describe(ids)

    async def describe_for_update(self, presentation_id: UUID) -> StockedPresentation | None:
        self.calls.append(("describe_for_update", self._transactions.inside))
        found = await InMemoryPresentationCatalog.describe(self, [presentation_id])
        return found.get(presentation_id)


@pytest.mark.parametrize("command", ["record", "threshold"])
async def test_the_catalog_lookup_is_locked_and_inside_the_transaction(command: str) -> None:
    transactions = TrackingTransactions()
    base = Inventory()
    catalog = TrackingCatalog(transactions, *base.catalog.presentations)
    store = InMemoryStock()
    clock = FixedClock()
    record = RecordMovement(
        stock=store,
        settings=InMemorySettings(store),
        catalog=catalog,
        transactions=transactions,
        events=RecordingEventPublisher(),
        clock=clock,
    )
    threshold = SetThreshold(
        stock=store,
        settings=InMemorySettings(store),
        catalog=catalog,
        transactions=transactions,
        events=RecordingEventPublisher(),
        clock=clock,
    )

    if command == "record":
        await record.execute(STOCKED, MovementRequest(kind="restock", quantity=1, reason="x"), "a")
    else:
        await threshold.execute(STOCKED, 5)

    assert catalog.calls == [("describe_for_update", True)]
