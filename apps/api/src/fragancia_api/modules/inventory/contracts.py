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
    kind: Literal["restock", "adjustment", "loss", "return", "count"]
    quantity: int = Field(ge=-1_000_000_000, le=1_000_000_000, examples=[10])
    reason: str = Field(max_length=1000, examples=["Compra al proveedor"])


class MovementCreated(BaseModel):
    id: UUID
    on_hand: int = Field(ge=0)


class StockResponse(BaseModel):
    """The stock of one presentation."""

    presentation_id: UUID
    perfume_id: UUID
    label: str = Field(examples=["Versace Eros EDT 100 ml"])
    on_hand: int = Field(ge=0)
    threshold_override: int | None
    effective_threshold: int
    is_low: bool


class StockMovement(BaseModel):
    """One line of the ledger."""

    id: UUID
    kind: Literal["restock", "adjustment", "loss", "return", "count"]
    quantity: int
    delta: int
    on_hand_after: int = Field(ge=0)
    reason: str
    actor_id: str
    created_at: datetime


class StockMovementPage(Page[StockMovement]):
    """One page of a presentation's movements, newest first."""


class StockLine(BaseModel):
    """One in-stock presentation in the stock list."""

    presentation_id: UUID
    perfume_id: UUID
    label: str
    on_hand: int = Field(ge=0)
    effective_threshold: int
    is_low: bool


class StockPage(Page[StockLine]):
    """One page of the stock list."""


class ThresholdRequest(BaseModel):
    """`null` removes the override: the global threshold applies again."""

    threshold: int | None = Field(ge=-1_000_000, le=1_000_000)


class InventorySettingsResponse(BaseModel):
    low_stock_threshold: int


class InventorySettingsRequest(BaseModel):
    low_stock_threshold: int = Field(ge=-1_000_000, le=1_000_000)
