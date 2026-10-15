"""Commands of reservations: units held while a customer pays (decisions 3-5, 24-28).

Lock order (every command keeps it, so no wait cycle forms):

- `ReserveStock`: the catalog presentations (share), the settings row (share), then the stock
  items, all in `presentation_id` order. The new reservation row is invisible to others until
  the transaction commits. The catalog's writers keep the same order: the single perfume save
  writes its presentations in `presentation_id` order, and the import confirm locks every
  presentation it will write by id first (`PerfumeRepository.lock_presentations`). Neither
  forms a wait cycle with a multi-line reserve.
- The other commands (extend, commit, release, expire): the reservation row(s) first, then the
  settings row (share), then the stock items in `presentation_id` order.
- Only reservation operations lock reservation rows, and every operation takes stock items in
  one global order (`presentation_id`), so none forms a wait cycle with `RecordMovement`,
  `SetThreshold` or `UpdateInventorySettings` (which take the settings row, then items in the
  same order).

Every command runs in one `transactions.run` and publishes `StockChanged` for each item it
changed, stamped with a `now` taken after the locks.
"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import structlog

from fragancia_api.modules.inventory.application.commands.stock import _require_stocked
from fragancia_api.modules.inventory.application.ports import PresentationCatalog
from fragancia_api.modules.inventory.domain.errors import (
    ReservationCommitted,
    ReservationNotFound,
    ReservationReferenceTaken,
    StockInsufficient,
    StockUnavailable,
)
from fragancia_api.modules.inventory.domain.repositories import (
    ReservationRepository,
    SettingsRepository,
    StockRepository,
)
from fragancia_api.modules.inventory.domain.reservation import (
    SALE_ACTOR,
    Reservation,
    ReservationStatus,
)
from fragancia_api.modules.inventory.domain.stock import Reason, StockItem
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

EXPIRE_BATCH = 100


class ReserveStock:
    """Command: hold units of one or more presentations until `expires_at`, all or nothing
    (decision 24). Returns the reservation id."""

    def __init__(
        self,
        *,
        reservations: ReservationRepository,
        stock: StockRepository,
        settings: SettingsRepository,
        catalog: PresentationCatalog,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._reservations = reservations
        self._stock = stock
        self._settings = settings
        self._catalog = catalog
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(
        self, reference: str, quantities: Mapping[UUID, int], expires_at: datetime
    ) -> Result[UUID, DomainError]:
        match Reservation.create(reference, quantities, expires_at, self._clock.now()):
            case Err(invalid):
                return Err(invalid)
            case Ok(reservation):
                pass

        async def work() -> Result[UUID, DomainError]:
            ids = [line.presentation_id for line in reservation.lines]  # ascending
            for presentation_id in ids:
                refusal = await _require_stocked(self._catalog, presentation_id)
                if refusal is not None:
                    return Err(refusal)
            global_threshold = (await self._settings.get_for_share()).low_stock_threshold
            items = await self._stock.get_many_for_update(ids, now=self._clock.now())
            now = self._clock.now()
            short: list[dict[str, object]] = []
            for line in reservation.lines:
                item = items[line.presentation_id]
                match item.hold(line.quantity):
                    case Err(_):
                        short.append(
                            {
                                "presentation_id": str(line.presentation_id),
                                "requested": line.quantity,
                                "available": item.available,
                            }
                        )
                    case Ok(_):
                        pass
            if short:
                # A standalone call rolls back everything. Inside a caller's transaction, the
                # empty stock rows the locks inserted remain. They read exactly like a
                # presentation never counted (0 on hand), so they are harmless.
                return Err(StockUnavailable(details={"lines": short}))
            if not await self._reservations.add(reservation):
                return Err(ReservationReferenceTaken())
            for item in items.values():
                await self._stock.save(item)
            await self._events.publish(
                [item.changed_event(global_threshold, now) for item in items.values()]
            )
            return Ok(reservation.id)

        return await self._transactions.run(work)


class ExtendReservation:
    """Command: move the expiry of an active reservation later (decision 26). Stock does not
    change, so no event is published."""

    def __init__(
        self,
        *,
        reservations: ReservationRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._reservations = reservations
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, reservation_id: UUID, expires_at: datetime
    ) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            reservation = await self._reservations.get_for_update(reservation_id)
            if reservation is None:
                return Err(ReservationNotFound())
            match reservation.extend(expires_at, self._clock.now()):
                case Err(refused):
                    return Err(refused)
                case Ok(_):
                    pass
            await self._reservations.save(reservation)
            return Ok(None)

        return await self._transactions.run(work)


class CommitReservation:
    """Command: an approved payment sells the reservation's units, one `sale` movement per
    presentation (decisions 25 and 27).

    Idempotent: committing a committed reservation is `Ok` and changes nothing (payment
    webhooks retry). A reservation that still holds its units needs only the units on hand
    (`INVENTORY_STOCK_INSUFFICIENT` when a loss left fewer); a late commit (expired or released)
    needs them available (`INVENTORY_STOCK_UNAVAILABLE`). A refusal lists every short line,
    changes nothing and leaves the reservation's status: the caller decides."""

    def __init__(
        self,
        *,
        reservations: ReservationRepository,
        stock: StockRepository,
        settings: SettingsRepository,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._reservations = reservations
        self._stock = stock
        self._settings = settings
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(self, reservation_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            reservation = await self._reservations.get_for_update(reservation_id)
            if reservation is None:
                return Err(ReservationNotFound())
            if reservation.status is ReservationStatus.COMMITTED:
                return Ok(None)
            match Reason.create(reservation.sale_reason()):
                case Err(invalid):
                    return Err(invalid)
                case Ok(reason):
                    pass
            global_threshold = (await self._settings.get_for_share()).low_stock_threshold
            items = await self._stock.get_many_for_update(
                [line.presentation_id for line in reservation.lines], now=self._clock.now()
            )
            now = self._clock.now()
            held = reservation.holds_units(now)
            short: list[dict[str, object]] = []
            for line in reservation.lines:
                item = items[line.presentation_id]
                match item.sell(line.quantity, reason, SALE_ACTOR, now, held=held):
                    case Err(StockInsufficient()):
                        short.append(
                            {
                                "presentation_id": str(line.presentation_id),
                                "requested": line.quantity,
                                "on_hand": item.on_hand,
                            }
                        )
                    case Err(_):
                        short.append(
                            {
                                "presentation_id": str(line.presentation_id),
                                "requested": line.quantity,
                                "available": item.available,
                            }
                        )
                    case Ok(_):
                        pass
            if short:
                refusal: DomainError = (
                    StockInsufficient(details={"lines": short})
                    if held
                    else StockUnavailable(details={"lines": short})
                )
                return Err(refusal)
            reservation.commit(now)
            await self._reservations.save(reservation)
            for item in items.values():
                await self._stock.save(item)
            await self._events.publish(
                [item.changed_event(global_threshold, now) for item in items.values()]
            )
            return Ok(None)

        return await self._transactions.run(work)


class ReleaseReservation:
    """Command: a rejected payment gives the units back. Releasing a released or expired
    reservation is `Ok` and changes nothing; a committed one is refused
    (`INVENTORY_RESERVATION_COMMITTED`). Records no movement (decision 27)."""

    def __init__(
        self,
        *,
        reservations: ReservationRepository,
        stock: StockRepository,
        settings: SettingsRepository,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._reservations = reservations
        self._stock = stock
        self._settings = settings
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(self, reservation_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            reservation = await self._reservations.get_for_update(reservation_id)
            if reservation is None:
                return Err(ReservationNotFound())
            match reservation.status:
                case ReservationStatus.COMMITTED:
                    return Err(ReservationCommitted())
                case ReservationStatus.RELEASED | ReservationStatus.EXPIRED:
                    return Ok(None)
                case ReservationStatus.ACTIVE:
                    pass
            global_threshold = (await self._settings.get_for_share()).low_stock_threshold
            items = await self._stock.get_many_for_update(
                [line.presentation_id for line in reservation.lines], now=self._clock.now()
            )
            now = self._clock.now()
            _unhold(reservation, items)
            reservation.release(now)
            await self._reservations.save(reservation)
            for item in items.values():
                await self._stock.save(item)
            await self._events.publish(
                [item.changed_event(global_threshold, now) for item in items.values()]
            )
            return Ok(None)

        return await self._transactions.run(work)


class ExpireReservations:
    """Worker job: expire active reservations past their expiry, a batch at a time, and give
    their units back. Returns how many reservations expired."""

    def __init__(
        self,
        *,
        reservations: ReservationRepository,
        stock: StockRepository,
        settings: SettingsRepository,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._reservations = reservations
        self._stock = stock
        self._settings = settings
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def run_once(self) -> int:
        async def work() -> Result[int, DomainError]:
            due = await self._reservations.claim_expired(self._clock.now(), EXPIRE_BATCH)
            if not due:
                return Ok(0)
            global_threshold = (await self._settings.get_for_share()).low_stock_threshold
            items = await self._stock.get_many_for_update(
                {line.presentation_id for reservation in due for line in reservation.lines},
                now=self._clock.now(),
            )
            now = self._clock.now()
            for reservation in due:
                _unhold(reservation, items)
                reservation.expire(now)
                await self._reservations.save(reservation)
            for item in items.values():
                await self._stock.save(item)
            await self._events.publish(
                [item.changed_event(global_threshold, now) for item in items.values()]
            )
            _log.info("inventory.reservations_expired", count=len(due))
            return Ok(len(due))

        match await self._transactions.run(work):
            case Err(_):
                return 0
            case Ok(expired):
                return expired


def _unhold(reservation: Reservation, items: Mapping[UUID, StockItem]) -> None:
    for line in reservation.lines:
        items[line.presentation_id].unhold(line.quantity)
