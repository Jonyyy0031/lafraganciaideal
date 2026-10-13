from uuid import UUID

from fragancia_api.modules.inventory.application.ports import PresentationCatalog, StockQueries
from fragancia_api.modules.inventory.contracts import (
    InventorySettingsResponse,
    StockLine,
    StockMovementPage,
    StockPage,
    StockResponse,
)
from fragancia_api.modules.inventory.domain.errors import (
    PresentationNotFound,
    PresentationNotStocked,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class GetStock:
    """Query: the stock, thresholds and low flag of one in-stock presentation."""

    def __init__(self, catalog: PresentationCatalog, queries: StockQueries) -> None:
        self._catalog = catalog
        self._queries = queries

    async def execute(self, presentation_id: UUID) -> Result[StockResponse, DomainError]:
        found = (await self._catalog.describe([presentation_id])).get(presentation_id)
        if found is None:
            return Err(PresentationNotFound())
        if not found.stocked:
            return Err(PresentationNotStocked())
        on_hand, override = await self._queries.get(presentation_id) or (0, None)
        threshold = override if override is not None else await self._queries.low_stock_threshold()
        return Ok(
            StockResponse(
                presentation_id=presentation_id,
                perfume_id=found.perfume_id,
                label=found.label,
                on_hand=on_hand,
                threshold_override=override,
                effective_threshold=threshold,
                is_low=on_hand <= threshold,
            )
        )


class ListMovements:
    """Query: the ledger of one presentation, newest first."""

    def __init__(self, catalog: PresentationCatalog, queries: StockQueries) -> None:
        self._catalog = catalog
        self._queries = queries

    async def execute(
        self, presentation_id: UUID, *, page: int, size: int
    ) -> Result[StockMovementPage, DomainError]:
        if presentation_id not in await self._catalog.describe([presentation_id]):
            return Err(PresentationNotFound())
        return Ok(await self._queries.movements(presentation_id, page=page, size=size))


class ListStock:
    """Query: every in-stock presentation with its units, optionally only the low ones.

    The catalog gives the presentations, the stock rows are joined in memory and then filtered
    and paginated: a shop's catalog is a few hundred presentations. A presentation never counted
    shows with 0 on hand.
    """

    def __init__(self, catalog: PresentationCatalog, queries: StockQueries) -> None:
        self._catalog = catalog
        self._queries = queries

    async def execute(self, *, low: bool, page: int, size: int) -> StockPage:
        presentations = await self._catalog.in_stock()
        counted = await self._queries.on_hand_by_id([p.presentation_id for p in presentations])
        global_threshold = await self._queries.low_stock_threshold()
        lines: list[StockLine] = []
        for presentation in presentations:
            on_hand, override = counted.get(presentation.presentation_id, (0, None))
            threshold = override if override is not None else global_threshold
            lines.append(
                StockLine(
                    presentation_id=presentation.presentation_id,
                    perfume_id=presentation.perfume_id,
                    label=presentation.label,
                    on_hand=on_hand,
                    effective_threshold=threshold,
                    is_low=on_hand <= threshold,
                )
            )
        if low:
            lines = [line for line in lines if line.is_low]
        start = (page - 1) * size
        return StockPage(items=lines[start : start + size], total=len(lines), page=page, size=size)


class GetInventorySettings:
    """Query: the global inventory settings."""

    def __init__(self, queries: StockQueries) -> None:
        self._queries = queries

    async def execute(self) -> InventorySettingsResponse:
        return InventorySettingsResponse(
            low_stock_threshold=await self._queries.low_stock_threshold()
        )
