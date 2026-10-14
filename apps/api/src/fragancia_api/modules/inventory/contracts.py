"""HTTP contracts of inventory (source of truth for OpenAPI and the generated web client)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from fragancia_api.shared.contracts import Page


class MovementRequest(BaseModel):
    """One stock movement. `quantity` is positive for restock, return and loss, signed and
    non-zero for an adjustment, and the absolute count (0 or more) for a count."""

    # Business rules (ranges, reason length) live in the domain; this only bounds the payload.
    # No "sale": a sale only comes from committing a reservation.
    kind: Literal["restock", "adjustment", "loss", "return", "count"]
    quantity: int = Field(ge=-1_000_000_000, le=1_000_000_000, examples=[10])
    reason: str = Field(max_length=1000, examples=["Compra al proveedor"])


class MovementCreated(BaseModel):
    id: UUID
    on_hand: int = Field(ge=0)


class StockResponse(BaseModel):
    """The stock of one presentation. `available` is `on_hand - reserved`, never below 0;
    `is_low` is `available <= effective_threshold`."""

    presentation_id: UUID
    perfume_id: UUID
    label: str = Field(examples=["Versace Eros EDT 100 ml"])
    on_hand: int = Field(ge=0)
    reserved: int = Field(ge=0)
    available: int = Field(ge=0)
    threshold_override: int | None
    effective_threshold: int
    is_low: bool


class StockMovement(BaseModel):
    """One line of the ledger."""

    id: UUID
    kind: Literal["restock", "adjustment", "loss", "return", "count", "sale"]
    quantity: int
    delta: int
    on_hand_after: int = Field(ge=0)
    reason: str
    actor_id: str
    created_at: datetime


class StockMovementPage(Page[StockMovement]):
    """One page of a presentation's movements, newest first."""


class StockLine(BaseModel):
    """One in-stock presentation in the stock list. `available` is `on_hand - reserved`, never
    below 0; `is_low` is `available <= effective_threshold`."""

    presentation_id: UUID
    perfume_id: UUID
    label: str
    on_hand: int = Field(ge=0)
    reserved: int = Field(ge=0)
    available: int = Field(ge=0)
    effective_threshold: int
    is_low: bool


class StockPage(Page[StockLine]):
    """One page of the stock list."""


class ReservationHold(BaseModel):
    """An active reservation holding units of one presentation."""

    reservation_id: UUID
    reference: str = Field(examples=["PED-1042"])
    quantity: int = Field(ge=1)
    expires_at: datetime
    created_at: datetime


class ReservationHoldPage(Page[ReservationHold]):
    """One page of a presentation's active reservations, soonest expiry first."""


class ThresholdRequest(BaseModel):
    """`null` removes the override: the global threshold applies again."""

    threshold: int | None = Field(ge=-1_000_000, le=1_000_000)


class InventorySettingsResponse(BaseModel):
    low_stock_threshold: int


class InventorySettingsRequest(BaseModel):
    low_stock_threshold: int = Field(ge=-1_000_000, le=1_000_000)
