from uuid import UUID

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
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


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
        clock: Clock,
    ) -> None:
        self._stock = stock
        self._settings = settings
        self._catalog = catalog
        self._transactions = transactions
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
            # Lock order: the catalog presentation (share), then the stock item.
            refusal = await _require_stocked(self._catalog, presentation_id)
            if refusal is not None:
                return Err(refusal)
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
            return Ok(MovementCreated(id=movement.id, on_hand=item.on_hand))

        return await self._transactions.run(work)


class SetThreshold:
    """Command: set or clear the low-stock threshold of one presentation."""

    def __init__(
        self,
        *,
        stock: StockRepository,
        catalog: PresentationCatalog,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._stock = stock
        self._catalog = catalog
        self._transactions = transactions
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
            refusal = await _require_stocked(self._catalog, presentation_id)
            if refusal is not None:
                return Err(refusal)
            item = await self._stock.get_for_update(presentation_id, now=self._clock.now())
            # Taken under the stock lock, so updated_at follows the order changes applied.
            now = self._clock.now()
            if item is None:
                item = StockItem.empty(presentation_id, now)
            item.set_threshold(override)
            await self._stock.save(item)
            return Ok(None)

        return await self._transactions.run(work)


class UpdateInventorySettings:
    """Command: set the global low-stock threshold."""

    def __init__(self, *, settings: SettingsRepository, transactions: TransactionRunner) -> None:
        self._settings = settings
        self._transactions = transactions

    async def execute(self, low_stock_threshold: int) -> Result[None, DomainError]:
        match Threshold.create(low_stock_threshold):
            case Err(invalid):
                return Err(invalid)
            case Ok(threshold):
                pass

        async def work() -> Result[None, DomainError]:
            await self._settings.save(InventorySettings(low_stock_threshold=threshold))
            return Ok(None)

        return await self._transactions.run(work)
