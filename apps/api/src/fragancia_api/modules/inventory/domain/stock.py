"""The StockItem aggregate and its ledger of movements.

One stock item per in-stock presentation, created by its first movement (a presentation
without one has 0 on hand). Every change is an append-only `Movement`; `on_hand` never goes
below 0, so a movement that would make it negative is refused.

| kind       | quantity       | effect                       |
| ---------- | -------------- | ---------------------------- |
| restock    | 1..max         | adds                         |
| return     | 1..max         | adds                         |
| loss       | 1..max         | subtracts                    |
| adjustment | -max..max, != 0| adds or subtracts            |
| count      | 0..max         | sets `on_hand` to the number |

A presentation is low when `on_hand <= effective threshold`: its own override, else the global
one.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from fragancia_api.modules.inventory.domain.errors import (
    StockInsufficient,
    StockQuantityInvalid,
    StockReasonInvalid,
    ThresholdInvalid,
)
from fragancia_api.shared.kernel import AggregateRoot, Err, Ok, Result, new_id

__all__ = [
    "DEFAULT_LOW_STOCK_THRESHOLD",
    "QUANTITY_MAX",
    "InventorySettings",
    "Movement",
    "MovementKind",
    "Reason",
    "StockItem",
    "Threshold",
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
        case MovementKind.RESTOCK | MovementKind.RETURN | MovementKind.LOSS:
            return 1 <= quantity <= QUANTITY_MAX
        case MovementKind.ADJUSTMENT:
            return quantity != 0 and abs(quantity) <= QUANTITY_MAX
        case MovementKind.COUNT:
            return 0 <= quantity <= QUANTITY_MAX


def effective_threshold(override: int | None, global_threshold: int) -> int:
    """The threshold that applies: the presentation's own override, else the global one."""
    return override if override is not None else global_threshold


def is_low(on_hand: int, override: int | None, global_threshold: int) -> bool:
    """The one low-stock rule: `on_hand <= effective threshold` (equality is low)."""
    return on_hand <= effective_threshold(override, global_threshold)


class StockItem(AggregateRoot):
    def __init__(
        self,
        *,
        presentation_id: UUID,
        on_hand: int,
        threshold_override: Threshold | None,
        updated_at: datetime,
    ) -> None:
        super().__init__()
        self.presentation_id = presentation_id
        self.on_hand = on_hand
        self.threshold_override = threshold_override
        self.updated_at = updated_at
        self.new_movements: list[Movement] = []

    @classmethod
    def empty(cls, presentation_id: UUID, now: datetime) -> StockItem:
        return cls(
            presentation_id=presentation_id, on_hand=0, threshold_override=None, updated_at=now
        )

    def record_movement(
        self,
        kind: MovementKind,
        quantity: int,
        reason: Reason,
        actor_id: str,
        now: datetime,
    ) -> Result[Movement, StockQuantityInvalid | StockInsufficient]:
        if not _quantity_valid(kind, quantity):
            return Err(StockQuantityInvalid(details={"kind": kind.value, "max": QUANTITY_MAX}))
        match kind:
            case MovementKind.RESTOCK | MovementKind.RETURN:
                delta = quantity
            case MovementKind.LOSS:
                delta = -quantity
            case MovementKind.ADJUSTMENT:
                delta = quantity
            case MovementKind.COUNT:
                delta = quantity - self.on_hand
        on_hand_after = self.on_hand + delta
        if on_hand_after < 0:
            return Err(StockInsufficient(details={"on_hand": self.on_hand}))
        movement = Movement(
            id=new_id(),
            presentation_id=self.presentation_id,
            kind=kind,
            quantity=quantity,
            delta=delta,
            on_hand_after=on_hand_after,
            reason=reason,
            actor_id=actor_id,
            created_at=now,
        )
        self.on_hand = on_hand_after
        self.updated_at = now
        self.new_movements.append(movement)
        return Ok(movement)

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
            self.on_hand, override.value if override is not None else None, global_threshold.value
        )
