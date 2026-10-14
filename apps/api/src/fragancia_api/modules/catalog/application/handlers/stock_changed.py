"""Outbox subscriber that keeps the catalog's copy of the stock figures current.

It runs in the worker, inside the relay's transaction. Delivery is at-least-once and unordered,
so the copy keeps the newest `as_of` and an older event is a no-op.
"""

from datetime import datetime
from uuid import UUID

from fragancia_api.modules.catalog.application.ports import StockCopy
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Ok, Result

# The event is published by the inventory module; the catalog knows it by name only.
STOCK_CHANGED_EVENT = "inventory.stock.changed"


class ApplyStockChanged:
    def __init__(self, *, copy: StockCopy, transactions: TransactionRunner) -> None:
        self._copy = copy
        self._transactions = transactions

    async def __call__(self, message: EventMessage) -> None:
        payload = message.payload
        presentation_id = UUID(str(payload["presentation_id"]))
        available = int(str(payload["available"]))
        threshold = int(str(payload["threshold"]))
        as_of = datetime.fromisoformat(str(payload["as_of"]))
        if as_of.tzinfo is None:
            raise ValueError("as_of must carry a timezone")

        async def work() -> Result[None, DomainError]:
            await self._copy.apply(presentation_id, available, threshold, as_of)
            return Ok(None)

        await self._transactions.run(work)
