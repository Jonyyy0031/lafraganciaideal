"""In-memory adapters for unit tests. They honor the same contracts as the SQL ones."""

from collections.abc import Iterable, Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.inventory.application.ports import StockedPresentation
from fragancia_api.modules.inventory.contracts import (
    ReservationHold,
    ReservationHoldPage,
    StockMovement,
    StockMovementPage,
)
from fragancia_api.modules.inventory.domain.reservation import Reservation, ReservationStatus
from fragancia_api.modules.inventory.domain.stock import (
    InventorySettings,
    Movement,
    StockItem,
)


class InMemoryReservations:
    """The `ReservationRepository` over a dict by id."""

    def __init__(self) -> None:
        self.by_id: dict[UUID, Reservation] = {}

    async def add(self, reservation: Reservation) -> bool:
        if any(r.reference == reservation.reference for r in self.by_id.values()):
            return False
        self.by_id[reservation.id] = deepcopy(reservation)
        return True

    async def get_for_update(self, reservation_id: UUID) -> Reservation | None:
        found = self.by_id.get(reservation_id)
        return deepcopy(found) if found is not None else None

    async def claim_expired(self, now: datetime, limit: int) -> list[Reservation]:
        due = sorted(
            (
                r
                for r in self.by_id.values()
                if r.status is ReservationStatus.ACTIVE and r.expires_at <= now
            ),
            key=lambda r: r.id,
        )
        return [deepcopy(r) for r in due[:limit]]

    async def save(self, reservation: Reservation) -> None:
        self.by_id[reservation.id] = deepcopy(reservation)


class InMemoryStock:
    """Both the repository and the queries over one store, plus the settings. `reservations`
    is the store `active_holds` reads (pass the same `InMemoryReservations` the commands use)."""

    def __init__(
        self,
        settings: InventorySettings | None = None,
        reservations: InMemoryReservations | None = None,
    ) -> None:
        self.items: dict[UUID, StockItem] = {}
        self.movements_log: list[Movement] = []
        self.settings = settings if settings is not None else InventorySettings()
        self.reservations = reservations if reservations is not None else InMemoryReservations()

    # StockRepository

    async def get_for_update(self, presentation_id: UUID, *, now: datetime) -> StockItem | None:
        item = self.items.get(presentation_id)
        return deepcopy(item) if item is not None else None

    async def get_many_for_update(
        self, presentation_ids: Iterable[UUID], *, now: datetime
    ) -> dict[UUID, StockItem]:
        found: dict[UUID, StockItem] = {}
        for presentation_id in sorted(set(presentation_ids)):
            item = self.items.get(presentation_id)
            found[presentation_id] = (
                deepcopy(item) if item is not None else StockItem.empty(presentation_id, now)
            )
        return found

    async def list_all_for_update(self) -> list[StockItem]:
        return [deepcopy(self.items[key]) for key in sorted(self.items)]

    async def save(self, item: StockItem) -> None:
        self.movements_log.extend(item.new_movements)
        item.new_movements.clear()
        self.items[item.presentation_id] = deepcopy(item)

    # StockQueries

    async def get(self, presentation_id: UUID) -> tuple[int, int, int | None] | None:
        item = self.items.get(presentation_id)
        if item is None:
            return None
        override = item.threshold_override
        return (item.on_hand, item.reserved, override.value if override is not None else None)

    async def movements(self, presentation_id: UUID, *, page: int, size: int) -> StockMovementPage:
        own = sorted(
            (m for m in self.movements_log if m.presentation_id == presentation_id),
            key=lambda m: (m.created_at, m.id),
            reverse=True,
        )
        start = (page - 1) * size
        items = [
            StockMovement(
                id=m.id,
                kind=m.kind.value,
                quantity=m.quantity,
                delta=m.delta,
                on_hand_after=m.on_hand_after,
                reason=m.reason.value,
                actor_id=m.actor_id,
                created_at=m.created_at,
            )
            for m in own[start : start + size]
        ]
        return StockMovementPage(items=items, total=len(own), page=page, size=size)

    async def on_hand_by_id(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, int | None]]:
        found: dict[UUID, tuple[int, int, int | None]] = {}
        for presentation_id in ids:
            counted = await self.get(presentation_id)
            if counted is not None:
                found[presentation_id] = counted
        return found

    async def active_holds(
        self, presentation_id: UUID, *, page: int, size: int
    ) -> ReservationHoldPage:
        holds = sorted(
            (
                ReservationHold(
                    reservation_id=r.id,
                    reference=r.reference,
                    quantity=line.quantity,
                    expires_at=r.expires_at,
                    created_at=r.created_at,
                )
                for r in self.reservations.by_id.values()
                if r.status is ReservationStatus.ACTIVE
                for line in r.lines
                if line.presentation_id == presentation_id
            ),
            key=lambda h: (h.expires_at, h.reservation_id),
        )
        start = (page - 1) * size
        return ReservationHoldPage(
            items=holds[start : start + size], total=len(holds), page=page, size=size
        )

    async def low_stock_threshold(self) -> int:
        return self.settings.low_stock_threshold.value


class InMemorySettings:
    """The `SettingsRepository` over the settings of an `InMemoryStock`."""

    def __init__(self, stock: InMemoryStock) -> None:
        self._stock = stock

    async def get(self) -> InventorySettings:
        return self._stock.settings

    async def get_for_update(self) -> InventorySettings:
        return self._stock.settings

    async def get_for_share(self) -> InventorySettings:
        return self._stock.settings

    async def save(self, settings: InventorySettings) -> None:
        self._stock.settings = settings


class InMemoryPresentationCatalog:
    """The `PresentationCatalog` over a fixed list, in display order."""

    def __init__(self, *presentations: StockedPresentation) -> None:
        self.presentations = list(presentations)

    async def describe(self, ids: Sequence[UUID]) -> dict[UUID, StockedPresentation]:
        wanted = set(ids)
        return {p.presentation_id: p for p in self.presentations if p.presentation_id in wanted}

    async def describe_for_update(self, presentation_id: UUID) -> StockedPresentation | None:
        return (await self.describe([presentation_id])).get(presentation_id)

    async def in_stock(self) -> list[StockedPresentation]:
        return [p for p in self.presentations if p.stocked]
