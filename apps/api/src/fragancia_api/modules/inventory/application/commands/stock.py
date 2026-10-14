from collections.abc import Mapping
from uuid import UUID

import structlog

from fragancia_api.modules.inventory.application.ports import PresentationCatalog
from fragancia_api.modules.inventory.contracts import MovementCreated, MovementRequest
from fragancia_api.modules.inventory.domain.errors import (
    PresentationNotFound,
    PresentationNotStocked,
)
from fragancia_api.modules.inventory.domain.repositories import (
    SettingsRepository,
    StockRepository,
)
from fragancia_api.modules.inventory.domain.stock import (
    InventorySettings,
    MovementKind,
    Reason,
    StockItem,
    Threshold,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

IMPORT_COUNT_REASON = "Conteo desde la importación de Excel"


async def _require_stocked(
    catalog: PresentationCatalog, presentation_id: UUID
) -> PresentationNotFound | PresentationNotStocked | None:
    found = await catalog.describe_for_update(presentation_id)
    if found is None:
        return PresentationNotFound()
    if not found.stocked:
        return PresentationNotStocked()
    return None


class RecordMovement:
    """Command: append a movement to the ledger of an in-stock presentation. The first
    movement creates the stock item."""

    def __init__(
        self,
        *,
        stock: StockRepository,
        settings: SettingsRepository,
        catalog: PresentationCatalog,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._stock = stock
        self._settings = settings
        self._catalog = catalog
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(
        self, presentation_id: UUID, request: MovementRequest, actor_id: str
    ) -> Result[MovementCreated, DomainError]:
        match Reason.create(request.reason):
            case Err(invalid):
                return Err(invalid)
            case Ok(reason):
                pass

        async def work() -> Result[MovementCreated, DomainError]:
            # Lock order (a settings change takes the settings row, then every stock item
            # by presentation_id, so no wait cycle forms): the catalog presentation (share),
            # the settings row (share), then the stock item.
            refusal = await _require_stocked(self._catalog, presentation_id)
            if refusal is not None:
                return Err(refusal)
            global_threshold = (await self._settings.get_for_share()).low_stock_threshold
            item = await self._stock.get_for_update(presentation_id, now=self._clock.now())
            # Taken under the stock lock, so ledger order matches the order movements applied.
            now = self._clock.now()
            if item is None:
                item = StockItem.empty(presentation_id, now)
            kind = MovementKind(request.kind)
            match item.record_movement(kind, request.quantity, reason, actor_id, now):
                case Err(refused):
                    return Err(refused)
                case Ok(movement):
                    pass
            await self._stock.save(item)
            await self._events.publish([item.changed_event(global_threshold, now)])
            return Ok(MovementCreated(id=movement.id, on_hand=item.on_hand))

        return await self._transactions.run(work)


class RecordImportedCounts:
    """Command: record one physical count per presentation of an applied catalog import.

    A count that cannot be recorded (the presentation is gone, is no longer in stock, or the
    movement is refused) is logged and skipped; the others are still recorded. A count equal
    to `on_hand` is recorded too (decision 20)."""

    def __init__(
        self,
        *,
        stock: StockRepository,
        settings: SettingsRepository,
        catalog: PresentationCatalog,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._stock = stock
        self._settings = settings
        self._catalog = catalog
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(self, counts: Mapping[UUID, int], actor_id: str) -> Result[int, DomainError]:
        match Reason.create(IMPORT_COUNT_REASON):
            case Err(invalid):
                return Err(invalid)
            case Ok(reason):
                pass

        async def work() -> Result[int, DomainError]:
            # Lock order, as in `RecordMovement`: the catalog presentation (share), the
            # settings row (share), then the stock item. Items go in `presentation_id` order,
            # as a settings change takes them, so no wait cycle forms.
            recorded = 0
            global_threshold: Threshold | None = None
            for presentation_id in sorted(counts):
                refusal = await _require_stocked(self._catalog, presentation_id)
                if refusal is not None:
                    _log.warning(
                        "inventory.import_count_skipped",
                        presentation_id=str(presentation_id),
                        code=refusal.code,
                    )
                    continue
                if global_threshold is None:
                    global_threshold = (await self._settings.get_for_share()).low_stock_threshold
                item = await self._stock.get_for_update(presentation_id, now=self._clock.now())
                # Taken under the stock lock, so ledger order matches the order movements applied.
                now = self._clock.now()
                if item is None:
                    item = StockItem.empty(presentation_id, now)
                match item.record_movement(
                    MovementKind.COUNT, counts[presentation_id], reason, actor_id, now
                ):
                    case Err(refused):
                        _log.warning(
                            "inventory.import_count_skipped",
                            presentation_id=str(presentation_id),
                            code=refused.code,
                        )
                        continue
                    case Ok(_):
                        pass
                await self._stock.save(item)
                await self._events.publish([item.changed_event(global_threshold, now)])
                recorded += 1
            return Ok(recorded)

        return await self._transactions.run(work)


class SetThreshold:
    """Command: set or clear the low-stock threshold of one presentation."""

    def __init__(
        self,
        *,
        stock: StockRepository,
        settings: SettingsRepository,
        catalog: PresentationCatalog,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._stock = stock
        self._settings = settings
        self._catalog = catalog
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(
        self, presentation_id: UUID, threshold: int | None
    ) -> Result[None, DomainError]:
        override: Threshold | None = None
        if threshold is not None:
            match Threshold.create(threshold):
                case Err(invalid):
                    return Err(invalid)
                case Ok(valid):
                    override = valid

        async def work() -> Result[None, DomainError]:
            # Same lock order as `RecordMovement`: presentation (share), settings (share),
            # then the stock item.
            refusal = await _require_stocked(self._catalog, presentation_id)
            if refusal is not None:
                return Err(refusal)
            global_threshold = (await self._settings.get_for_share()).low_stock_threshold
            item = await self._stock.get_for_update(presentation_id, now=self._clock.now())
            # Taken under the stock lock, so updated_at follows the order changes applied.
            now = self._clock.now()
            if item is None:
                item = StockItem.empty(presentation_id, now)
            item.set_threshold(override)
            await self._stock.save(item)
            await self._events.publish([item.changed_event(global_threshold, now)])
            return Ok(None)

        return await self._transactions.run(work)


class UpdateInventorySettings:
    """Command: set the global low-stock threshold."""

    def __init__(
        self,
        *,
        settings: SettingsRepository,
        stock: StockRepository,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._stock = stock
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(self, low_stock_threshold: int) -> Result[None, DomainError]:
        match Threshold.create(low_stock_threshold):
            case Err(invalid):
                return Err(invalid)
            case Ok(threshold):
                pass

        async def work() -> Result[None, DomainError]:
            # Lock order: the settings row (update), then every stock item by
            # presentation_id. It waits for movements holding the settings share lock, and
            # they wait for it, so no cycle forms and every committed count is seen.
            await self._settings.get_for_update()
            await self._settings.save(InventorySettings(low_stock_threshold=threshold))
            items = await self._stock.list_all_for_update()
            # Taken under the locks, so no later-stamped event can carry an older count.
            now = self._clock.now()
            # The effective threshold of every item without an override changed.
            await self._events.publish([item.changed_event(threshold, now) for item in items])
            return Ok(None)

        return await self._transactions.run(work)
