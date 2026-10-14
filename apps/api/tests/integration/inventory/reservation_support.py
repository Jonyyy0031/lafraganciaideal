"""Builders for the reservation integration tests: the commands over the real SQL adapters with a
chosen event publisher (so a test can pause a transaction on a gate), and small SQL readers."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.catalog.infrastructure.sql_presentation_directory import (
    SqlPresentationDirectory,
)
from fragancia_api.modules.inventory.application.commands.reservations import (
    CommitReservation,
    ExpireReservations,
    ExtendReservation,
    ReleaseReservation,
    ReserveStock,
)
from fragancia_api.modules.inventory.application.ports import PresentationCatalog
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_reservation_repository import (
    SqlReservationRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Ok, Result


def in_minutes(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


class Commands:
    """The reservation commands wired over the real adapters and `events`."""

    def __init__(
        self,
        container: Container,
        events: EventPublisher,
        catalog: PresentationCatalog | None = None,
    ) -> None:
        database = container.database
        reservations = SqlReservationRepository(database)
        stock = SqlStockRepository(database)
        settings = SqlSettingsRepository(database)
        transactions = SqlTransactionRunner(database)
        clock = SystemClock()
        catalog = catalog or CatalogPresentations(
            PresentationDirectory(SqlPresentationDirectory(database))
        )
        self.reserve = ReserveStock(
            reservations=reservations,
            stock=stock,
            settings=settings,
            catalog=catalog,
            transactions=transactions,
            events=events,
            clock=clock,
        )
        self.extend = ExtendReservation(
            reservations=reservations, transactions=transactions, clock=clock
        )
        self.commit = CommitReservation(
            reservations=reservations,
            stock=stock,
            settings=settings,
            transactions=transactions,
            events=events,
            clock=clock,
        )
        self.release = ReleaseReservation(
            reservations=reservations,
            stock=stock,
            settings=settings,
            transactions=transactions,
            events=events,
            clock=clock,
        )
        self.expire = ExpireReservations(
            reservations=reservations,
            stock=stock,
            settings=settings,
            transactions=transactions,
            events=events,
            clock=clock,
        )


async def reserve_ok(
    container: Container,
    quantities: dict[UUID, int],
    reference: str,
    minutes: float = 10,
) -> UUID:
    result = await container.services.get(ReserveStock).execute(
        reference, quantities, in_minutes(minutes)
    )
    assert isinstance(result, Ok)
    value: UUID = result.value
    return value


def value_of[T](result: Result[T, Any]) -> T:
    assert isinstance(result, Ok)
    value: T = result.value
    return value


async def make_due(container: Container, reservation_id: UUID) -> None:
    """Move a reservation's expiry into the past (what waiting for it would do)."""
    async with container.database.engine.begin() as connection:
        found = (
            await connection.execute(
                text("SELECT count(*) FROM inventory.reservations WHERE id = :id"),
                {"id": reservation_id},
            )
        ).scalar_one()
        assert found == 1  # proves the WHERE before the UPDATE
        await connection.execute(
            text(
                "UPDATE inventory.reservations SET expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": reservation_id},
        )


async def reservation_row(container: Container, reservation_id: UUID) -> dict[str, Any]:
    async with container.database.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT * FROM inventory.reservations WHERE id = :id"),
                    {"id": reservation_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def item_row(container: Container, presentation_id: UUID) -> tuple[int, int]:
    """`(on_hand, reserved)` straight from the table."""
    async with container.database.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT on_hand, reserved FROM inventory.stock_items WHERE presentation_id = :p"
                ),
                {"p": presentation_id},
            )
        ).one()
    return row.on_hand, row.reserved


async def table_count(container: Container, table: str) -> int:
    queries = {
        "reservations": "SELECT count(*) FROM inventory.reservations",
        "reservation_lines": "SELECT count(*) FROM inventory.reservation_lines",
        "stock_items": "SELECT count(*) FROM inventory.stock_items",
        "stock_movements": "SELECT count(*) FROM inventory.stock_movements",
    }
    async with container.database.engine.connect() as connection:
        return int((await connection.execute(text(queries[table]))).scalar_one())


async def sale_movements(container: Container, presentation_id: UUID) -> list[dict[str, Any]]:
    async with container.database.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT kind, quantity, delta, on_hand_after, reason, actor_id "
                    "FROM inventory.stock_movements WHERE presentation_id = :p AND kind = 'sale'"
                ),
                {"p": presentation_id},
            )
        ).mappings()
        return [dict(row) for row in rows]
