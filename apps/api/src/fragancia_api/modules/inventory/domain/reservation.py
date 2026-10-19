"""The Reservation aggregate: units of one or more presentations held until an expiry.

A reservation is all or nothing (decision 24). It moves from `active` to `committed` (an
approved payment), `released` (a rejected payment) or `expired` (the job, at its expiry). A
committed one can end `returned` when its units go back on hand (a paid order cancelled). An
expired or released reservation can still be committed when a payment arrives late (decision
25); the command decides whether the units are still there.

These methods never touch stock: the commands pair them with `StockItem.hold`, `unhold` and
`sell` under the stock locks.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from fragancia_api.modules.inventory.domain.errors import (
    ReservationExpiryInvalid,
    ReservationInvalid,
    ReservationNotActive,
)
from fragancia_api.modules.inventory.domain.stock import QUANTITY_MAX
from fragancia_api.shared.kernel import AggregateRoot, Err, Ok, Result, new_id

__all__ = [
    "RESERVATION_REFERENCE_MAX",
    "SALE_ACTOR",
    "Reservation",
    "ReservationLine",
    "ReservationStatus",
]

RESERVATION_REFERENCE_MAX = 64
# Decision 27: the system is the actor of a sale.
SALE_ACTOR = "system"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"
    RETURNED = "returned"


@dataclass(frozen=True, slots=True)
class ReservationLine:
    presentation_id: UUID
    quantity: int


class Reservation(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        reference: str,
        lines: tuple[ReservationLine, ...],
        status: ReservationStatus,
        expires_at: datetime,
        created_at: datetime,
        closed_at: datetime | None,
    ) -> None:
        super().__init__()
        self.id = id
        self.reference = reference
        self.lines = tuple(sorted(lines, key=lambda line: line.presentation_id))
        self.status = status
        self.expires_at = expires_at
        self.created_at = created_at
        self.closed_at = closed_at

    @classmethod
    def create(
        cls,
        reference: str,
        quantities: Mapping[UUID, int],
        expires_at: datetime,
        now: datetime,
    ) -> Result[Reservation, ReservationInvalid | ReservationExpiryInvalid]:
        value = reference.strip()
        if not 1 <= len(value) <= RESERVATION_REFERENCE_MAX:
            return Err(ReservationInvalid(details={"reference_max": RESERVATION_REFERENCE_MAX}))
        if not quantities:
            return Err(ReservationInvalid(details={"lines_min": 1}))
        if any(not 1 <= quantity <= QUANTITY_MAX for quantity in quantities.values()):
            return Err(
                ReservationInvalid(details={"quantity_min": 1, "quantity_max": QUANTITY_MAX})
            )
        if expires_at <= now:
            return Err(ReservationExpiryInvalid())
        return Ok(
            cls(
                id=new_id(),
                reference=value,
                lines=tuple(
                    ReservationLine(presentation_id=presentation_id, quantity=quantity)
                    for presentation_id, quantity in quantities.items()
                ),
                status=ReservationStatus.ACTIVE,
                expires_at=expires_at,
                created_at=now,
                closed_at=None,
            )
        )

    def holds_units(self, now: datetime) -> bool:
        """Whether the stock items still count this reservation's units as reserved. An active
        reservation past its expiry still holds them until the job expires it."""
        del now  # The rule depends on the status only; `now` keeps the call site explicit.
        return self.status is ReservationStatus.ACTIVE

    def extend(
        self, expires_at: datetime, now: datetime
    ) -> Result[None, ReservationNotActive | ReservationExpiryInvalid]:
        if self.status is not ReservationStatus.ACTIVE or now >= self.expires_at:
            return Err(ReservationNotActive())
        if expires_at <= self.expires_at:
            return Err(ReservationExpiryInvalid())
        self.expires_at = expires_at
        return Ok(None)

    def commit(self, now: datetime) -> None:
        if self.status is ReservationStatus.COMMITTED:
            raise ValueError("The reservation is already committed")
        self.status = ReservationStatus.COMMITTED
        self.closed_at = now

    def mark_returned(self, now: datetime) -> None:
        """A sold reservation whose units went back on hand (a paid order cancelled)."""
        if self.status is not ReservationStatus.COMMITTED:
            raise ValueError("Only a committed reservation can be returned")
        self.status = ReservationStatus.RETURNED
        self.closed_at = now

    def release(self, now: datetime) -> None:
        if self.status is not ReservationStatus.ACTIVE:
            raise ValueError("Only an active reservation can be released")
        self.status = ReservationStatus.RELEASED
        self.closed_at = now

    def expire(self, now: datetime) -> None:
        if self.status is not ReservationStatus.ACTIVE:
            raise ValueError("Only an active reservation can expire")
        self.status = ReservationStatus.EXPIRED
        self.closed_at = now

    def sale_reason(self) -> str:
        """The ledger reason of its sale movements (Spanish ledger copy, decision 27)."""
        return f"Venta: {self.reference}"

    def return_reason(self) -> str:
        """The ledger reason of its return movements (Spanish ledger copy)."""
        return f"Devolución: {self.reference}"
