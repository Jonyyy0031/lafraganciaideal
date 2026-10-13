"""Plan 001, repair round 2 against `fragancia_test`: ledger order follows application order."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.modules.inventory.application.commands.stock import RecordMovement
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.domain.stock import StockItem
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows

pytestmark = pytest.mark.integration

START = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)


class AdvancingClock:
    """Every call returns a later instant, so any out-of-order read shows in the ledger."""

    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        value = START + timedelta(minutes=self.calls)
        self.calls += 1
        return value


class GatedStock(SqlStockRepository):
    """The first caller pauses just before taking the stock lock, after its command already
    read the clock; the gate opens when the test says so."""

    def __init__(self, database: object) -> None:
        super().__init__(database)  # type: ignore[arg-type]
        self.first_is_waiting = asyncio.Event()
        self.open_gate = asyncio.Event()
        self._calls = 0

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        self._calls += 1
        if self._calls == 1:
            self.first_is_waiting.set()
            await self.open_gate.wait()
        return await super().get_for_update(presentation_id, now=now)


async def test_ledger_order_by_created_at_follows_the_order_movements_were_applied(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """B reads the clock first but takes the lock second. Its timestamp must still be later."""
    _, [presentation_id] = await catalog_rows.perfume()
    database = container.database
    stock = GatedStock(database)
    record = RecordMovement(
        stock=stock,
        settings=SqlSettingsRepository(database),
        catalog=CatalogPresentations(PresentationDirectory(SqlPresentationDirectory(database))),
        transactions=SqlTransactionRunner(database),
        clock=AdvancingClock(),
    )

    async def restock(actor: str, quantity: int):  # type: ignore[no-untyped-def]
        return await record.execute(
            presentation_id,
            MovementRequest(kind="restock", quantity=quantity, reason="x"),
            actor,
        )

    first = asyncio.create_task(restock("first-to-read", 5))
    await asyncio.wait_for(stock.first_is_waiting.wait(), timeout=10)
    second = await restock("second-to-read", 7)  # reads later, locks first, finishes
    stock.open_gate.set()
    first_result = await asyncio.wait_for(first, timeout=10)

    assert isinstance(second, Ok)
    assert isinstance(first_result, Ok)
    async with database.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT actor_id, on_hand_after FROM inventory.stock_movements "
                    "WHERE presentation_id = :p ORDER BY created_at"
                ),
                {"p": presentation_id},
            )
        ).all()
    # Applied order: second-to-read (7) then first-to-read (7 + 5).
    assert [tuple(r) for r in rows] == [("second-to-read", 7), ("first-to-read", 12)]
