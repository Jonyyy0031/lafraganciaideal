"""The StockItem aggregate and its ledger of movements.

One stock item per in-stock presentation, created by its first movement (a presentation
without one has 0 on hand). Every change is an append-only `Movement`; `on_hand` never goes
below 0, so a movement that would make it negative is refused.

| kind       | quantity       | effect                                   |
| ---------- | -------------- | ---------------------------------------- |
| restock    | 1..max         | adds                                     |
| return     | 1..max         | adds                                     |
| loss       | 1..max         | subtracts                                |
| adjustment | -max..max, != 0| adds or subtracts                        |
| count      | 0..max         | sets `on_hand` to the number             |
| sale       | 1..max         | subtracts; only a reservation's commit   |

`reserved` counts the units that active reservations hold; `available` is
`max(on_hand - reserved, 0)`. A loss or count may leave `on_hand` below `reserved` (it reflects
the warehouse); `available` is then 0.

A presentation is low when `available <= effective threshold`: its own override, else the
global one.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self
from uuid import UUID

from fragancia_api.modules.inventory.domain.errors import (
    StockInsufficient,
    StockQuantityInvalid,
    StockReasonInvalid,
    StockUnavailable,
    ThresholdInvalid,
)
from fragancia_api.shared.kernel import (
    AggregateRoot,
    DomainEvent,
    Err,
    Ok,
    Result,
    new_id,
)

__all__ = [
    "DEFAULT_LOW_STOCK_THRESHOLD",
    "QUANTITY_MAX",
    "InventorySettings",
    "Movement",
    "MovementKind",
    "Reason",
    "StockChanged",
    "StockItem",
    "StockWentLow",
    "Threshold",
    "low_stock_event",
]

QUANTITY_MAX = 100_000
REASON_MAX_LENGTH = 200
THRESHOLD_MAX = 1000
DEFAULT_LOW_STOCK_THRESHOLD = 3


class MovementKind(StrEnum):
    RESTOCK = "restock"
    ADJUSTMENT = "adjustment"
    LOSS = "loss"
    RETURN = "return"
    COUNT = "count"
    SALE = "sale"


@dataclass(frozen=True, slots=True)
class Reason:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, StockReasonInvalid]:
        value = " ".join(raw.split())
        if not 1 <= len(value) <= REASON_MAX_LENGTH:
            return Err(StockReasonInvalid(details={"min": 1, "max": REASON_MAX_LENGTH}))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class Threshold:
    value: int

    @classmethod
    def create(cls, value: int) -> Result[Self, ThresholdInvalid]:
        if not 0 <= value <= THRESHOLD_MAX:
            return Err(ThresholdInvalid(details={"min": 0, "max": THRESHOLD_MAX}))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class InventorySettings:
    low_stock_threshold: Threshold = Threshold(DEFAULT_LOW_STOCK_THRESHOLD)


@dataclass(frozen=True, slots=True)
class Movement:
    """One line of the ledger. `quantity` is as sent; `delta` is the change it caused."""

    id: UUID
    presentation_id: UUID
    kind: MovementKind
    quantity: int
    delta: int
    on_hand_after: int
    reason: Reason
    actor_id: str
    created_at: datetime


def _quantity_valid(kind: MovementKind, quantity: int) -> bool:
    match kind:
        case MovementKind.RESTOCK | MovementKind.RETURN | MovementKind.LOSS | MovementKind.SALE:
            return 1 <= quantity <= QUANTITY_MAX
        case MovementKind.ADJUSTMENT:
            return quantity != 0 and abs(quantity) <= QUANTITY_MAX
        case MovementKind.COUNT:
            return 0 <= quantity <= QUANTITY_MAX


def effective_threshold(override: int | None, global_threshold: int) -> int:
    """The threshold that applies: the presentation's own override, else the global one."""
    return override if override is not None else global_threshold


def available_units(on_hand: int, reserved: int) -> int:
    """What can be bought: the units on hand that no reservation holds, never below 0."""
    return max(on_hand - reserved, 0)


def is_low(available: int, override: int | None, global_threshold: int) -> bool:
    """The one low-stock rule: `available <= effective threshold` (equality is low)."""
    return available <= effective_threshold(override, global_threshold)


@dataclass(frozen=True, kw_only=True)
class StockChanged(DomainEvent):
    """The stock figures of a presentation changed. `available` is what can be bought (on hand
    minus reserved, never below 0), `threshold` the effective low-stock threshold, `as_of` the
    time of the change (a consumer keeps the newest)."""

    name: ClassVar[str] = "inventory.stock.changed"
    presentation_id: UUID
    available: int
    threshold: int
    as_of: datetime


@dataclass(frozen=True, kw_only=True)
class StockWentLow(DomainEvent):
    """One or more presentations just dropped to or below their low-stock threshold, counted in
    units on hand. Each line is `{"presentation_id": str, "on_hand": int, "threshold": int}`."""

    name: ClassVar[str] = "inventory.stock.went_low"
    lines: tuple[dict[str, object], ...]


class StockItem(AggregateRoot):
    def __init__(
        self,
        *,
        presentation_id: UUID,
        on_hand: int,
        threshold_override: Threshold | None,
        updated_at: datetime,
        reserved: int = 0,
        low_alerted: bool = False,
    ) -> None:
        super().__init__()
        self.presentation_id = presentation_id
        self.on_hand = on_hand
        self.reserved = reserved
        self.threshold_override = threshold_override
        self.low_alerted = low_alerted
        self.updated_at = updated_at
        self.new_movements: list[Movement] = []

    @classmethod
    def empty(cls, presentation_id: UUID, now: datetime) -> StockItem:
        return cls(
            presentation_id=presentation_id,
            on_hand=0,
            threshold_override=None,
            updated_at=now,
            reserved=0,
        )

    @property
    def available(self) -> int:
        """What can be bought: `max(on_hand - reserved, 0)`."""
        return available_units(self.on_hand, self.reserved)

    def record_movement(
        self,
        kind: MovementKind,
        quantity: int,
        reason: Reason,
        actor_id: str,
        now: datetime,
    ) -> Result[Movement, StockQuantityInvalid | StockInsufficient]:
        # A sale only comes from committing a reservation (`sell`).
        if kind is MovementKind.SALE or not _quantity_valid(kind, quantity):
            return Err(StockQuantityInvalid(details={"kind": kind.value, "max": QUANTITY_MAX}))
        match kind:
            case MovementKind.RESTOCK | MovementKind.RETURN:
                delta = quantity
            case MovementKind.LOSS | MovementKind.SALE:
                delta = -quantity
            case MovementKind.ADJUSTMENT:
                delta = quantity
            case MovementKind.COUNT:
                delta = quantity - self.on_hand
        if self.on_hand + delta < 0:
            return Err(StockInsufficient(details={"on_hand": self.on_hand}))
        return Ok(self._append(kind, quantity, delta, reason, actor_id, now))

    def hold(self, quantity: int) -> Result[None, StockUnavailable]:
        """Hold units for a reservation; refused when more than `available` are asked."""
        if quantity > self.available:
            return Err(StockUnavailable())
        self.reserved += quantity
        return Ok(None)

    def unhold(self, quantity: int) -> None:
        """Give back units a reservation held (its release or expiry)."""
        if self.reserved - quantity < 0:
            raise ValueError("Cannot unhold more units than are reserved")
        self.reserved -= quantity

    def sell(
        self,
        quantity: int,
        reason: Reason,
        actor_id: str,
        now: datetime,
        *,
        held: bool,
    ) -> Result[Movement, StockInsufficient | StockUnavailable]:
        """Record the `sale` of one line of a committed reservation.

        `held`: the reservation still holds these units, so only `on_hand >= quantity` is
        required and the hold is consumed. Otherwise (a late commit) the units must be
        available."""
        if held:
            if self.on_hand < quantity:
                return Err(StockInsufficient(details={"on_hand": self.on_hand}))
            self.unhold(quantity)
        elif self.available < quantity:
            return Err(StockUnavailable())
        return Ok(self._append(MovementKind.SALE, quantity, -quantity, reason, actor_id, now))

    def _append(
        self,
        kind: MovementKind,
        quantity: int,
        delta: int,
        reason: Reason,
        actor_id: str,
        now: datetime,
    ) -> Movement:
        movement = Movement(
            id=new_id(),
            presentation_id=self.presentation_id,
            kind=kind,
            quantity=quantity,
            delta=delta,
            on_hand_after=self.on_hand + delta,
            reason=reason,
            actor_id=actor_id,
            created_at=now,
        )
        self.on_hand = movement.on_hand_after
        self.updated_at = now
        self.new_movements.append(movement)
        return movement

    def set_threshold(self, threshold: Threshold | None) -> None:
        self.threshold_override = threshold

    def effective_threshold(self, global_threshold: Threshold) -> int:
        override = self.threshold_override
        return effective_threshold(
            override.value if override is not None else None, global_threshold.value
        )

    def is_low(self, global_threshold: Threshold) -> bool:
        override = self.threshold_override
        return is_low(
            self.available, override.value if override is not None else None, global_threshold.value
        )

    def track_low(self, global_threshold: Threshold) -> bool:
        """Update the low-stock alert state; True when the item has just gone low.

        Uses `on_hand`, not `available` (decision 32): a reservation holds units but they are
        still in the warehouse, so reserving and releasing can never flap the alert. While the
        item stays low it is not alerted again; going above its threshold re-arms it."""
        low = self.on_hand <= self.effective_threshold(global_threshold)
        if low and not self.low_alerted:
            self.low_alerted = True
            return True
        if not low:
            self.low_alerted = False
        return False

    def changed_event(self, global_threshold: Threshold, as_of: datetime) -> StockChanged:
        return StockChanged(
            presentation_id=self.presentation_id,
            available=self.available,
            threshold=self.effective_threshold(global_threshold),
            as_of=as_of,
        )


def low_stock_event(items: Iterable[StockItem], global_threshold: Threshold) -> StockWentLow | None:
    """The one event for the items that just went low, or None when there are none."""
    lines = tuple(
        {
            "presentation_id": str(item.presentation_id),
            "on_hand": item.on_hand,
            "threshold": item.effective_threshold(global_threshold),
        }
        for item in items
    )
    return StockWentLow(lines=lines) if lines else None
