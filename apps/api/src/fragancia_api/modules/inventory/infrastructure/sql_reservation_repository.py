from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.dialects import postgresql

from fragancia_api.modules.inventory.domain.reservation import (
    Reservation,
    ReservationLine,
    ReservationStatus,
)
from fragancia_api.modules.inventory.infrastructure.tables import reservation_lines, reservations
from fragancia_api.shared.infrastructure.database import Database


def _to_reservation(row: RowMapping, lines: Sequence[ReservationLine]) -> Reservation:
    return Reservation(
        id=row["id"],
        reference=row["reference"],
        lines=tuple(lines),
        status=ReservationStatus(row["status"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


class SqlReservationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def add(self, reservation: Reservation) -> bool:
        session = self._database.session
        inserted = await session.scalar(
            postgresql.insert(reservations)
            .values(
                id=reservation.id,
                reference=reservation.reference,
                status=reservation.status.value,
                expires_at=reservation.expires_at,
                created_at=reservation.created_at,
                closed_at=reservation.closed_at,
            )
            .on_conflict_do_nothing(index_elements=[reservations.c.reference])
            .returning(reservations.c.id)
        )
        if inserted is None:
            return False
        await session.execute(
            insert(reservation_lines),
            [
                {
                    "reservation_id": reservation.id,
                    "presentation_id": line.presentation_id,
                    "quantity": line.quantity,
                }
                for line in reservation.lines
            ],
        )
        return True

    async def _lines(self, ids: Sequence[UUID]) -> dict[UUID, list[ReservationLine]]:
        statement = (
            select(reservation_lines)
            .where(reservation_lines.c.reservation_id.in_(list(ids)))
            .order_by(reservation_lines.c.reservation_id, reservation_lines.c.presentation_id)
        )
        rows = (await self._database.session.execute(statement)).mappings().all()
        found: dict[UUID, list[ReservationLine]] = {reservation_id: [] for reservation_id in ids}
        for row in rows:
            found[row["reservation_id"]].append(
                ReservationLine(presentation_id=row["presentation_id"], quantity=row["quantity"])
            )
        return found

    async def get_for_update(self, reservation_id: UUID) -> Reservation | None:
        statement = (
            select(reservations).where(reservations.c.id == reservation_id).with_for_update()
        )
        row = (await self._database.session.execute(statement)).mappings().first()
        if row is None:
            return None
        lines = await self._lines([reservation_id])
        return _to_reservation(row, lines[reservation_id])

    async def claim_expired(self, now: datetime, limit: int) -> list[Reservation]:
        statement = (
            select(reservations)
            .where(
                reservations.c.status == ReservationStatus.ACTIVE.value,
                reservations.c.expires_at <= now,
            )
            .order_by(reservations.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._database.session.execute(statement)).mappings().all()
        if not rows:
            return []
        lines = await self._lines([row["id"] for row in rows])
        return [_to_reservation(row, lines[row["id"]]) for row in rows]

    async def save(self, reservation: Reservation) -> None:
        await self._database.session.execute(
            update(reservations)
            .where(reservations.c.id == reservation.id)
            .values(
                status=reservation.status.value,
                expires_at=reservation.expires_at,
                closed_at=reservation.closed_at,
            )
        )
