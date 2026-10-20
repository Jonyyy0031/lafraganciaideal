"""Public facade of inventory for other modules: hold units while a customer pays."""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fragancia_api.shared.kernel import DomainError, Result

if TYPE_CHECKING:
    from fragancia_api.modules.inventory.application.commands.reservations import (
        CommitReservation,
        ExtendReservation,
        ReleaseReservation,
        ReserveStock,
        ReturnReservation,
    )


class StockReservations:
    """Command facade: the only way other modules (orders) reserve stock.

    Rules:
    - A reservation is all or nothing (decision 24): when any presentation lacks units, the
      whole reservation is refused with `INVENTORY_STOCK_UNAVAILABLE`, whose details list every
      short presentation (`presentation_id`, `requested`, `available`).
    - Inventory takes the expiry from the caller (decision 4: it depends on the payment
      method); an active reservation can be extended, an expired one cannot (decision 26).
    - Commit records one `sale` movement per presentation, with the reference as the reason and
      the system as the actor (decision 27). It is idempotent: committing a committed
      reservation is `Ok` and changes nothing. A late commit (expired or released) succeeds only
      while the units are available; otherwise it is refused and the reservation keeps its
      status (decision 25). A loss may leave fewer units on hand than a reservation holds
      (decision 28); its commit is then refused with `INVENTORY_STOCK_INSUFFICIENT`.
    - Release is idempotent too: releasing a released or expired reservation is `Ok`; a
      committed one is refused with `INVENTORY_RESERVATION_COMMITTED`. Reserving and releasing
      record no movement.
    - Return puts the units of a committed (sold) reservation back on hand, one `return`
      movement per presentation with the reason "Devolución: <reference>" and the system as the
      actor, and marks the reservation `returned`. It is idempotent on a returned reservation;
      any other status is refused with `INVENTORY_RESERVATION_NOT_COMMITTED`. Committing or
      releasing a returned reservation is refused.
    - An active reservation keeps its units until released, committed or expired by the job.

    Each call runs in its own transaction, or joins the caller's when one is active.
    """

    def __init__(
        self,
        *,
        reserve: ReserveStock,
        extend: ExtendReservation,
        commit: CommitReservation,
        release: ReleaseReservation,
        return_sold: ReturnReservation,
    ) -> None:
        self._reserve = reserve
        self._extend = extend
        self._commit = commit
        self._release = release
        self._return_sold = return_sold

    async def reserve(
        self, reference: str, quantities: Mapping[UUID, int], expires_at: datetime
    ) -> Result[UUID, DomainError]:
        """Hold `quantities` (presentation id to units) under the caller's unique `reference`
        until `expires_at`; returns the reservation id."""
        return await self._reserve.execute(reference, quantities, expires_at)

    async def extend(self, reservation_id: UUID, expires_at: datetime) -> Result[None, DomainError]:
        """Move the expiry of an active reservation later."""
        return await self._extend.execute(reservation_id, expires_at)

    async def commit(self, reservation_id: UUID) -> Result[None, DomainError]:
        """Sell the reserved units (an approved payment)."""
        return await self._commit.execute(reservation_id)

    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        """Give the units back (a rejected payment)."""
        return await self._release.execute(reservation_id)

    async def return_sold(self, reservation_id: UUID) -> Result[None, DomainError]:
        """Put the units of a sold reservation back on hand (a paid order cancelled)."""
        return await self._return_sold.execute(reservation_id)
