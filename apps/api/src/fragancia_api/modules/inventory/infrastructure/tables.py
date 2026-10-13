from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, Table
from sqlalchemy.types import Uuid

from fragancia_api.shared.infrastructure.tables import metadata

SCHEMA = "inventory"

stock_items = Table(
    "stock_items",
    metadata,
    # The catalog's presentation id: not a foreign key, modules own their schemas.
    Column("presentation_id", Uuid, primary_key=True),
    Column("on_hand", Integer, nullable=False),
    Column("threshold_override", Integer, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("on_hand >= 0", name="on_hand_not_negative"),
    schema=SCHEMA,
)

# Append-only ledger: rows are never updated or deleted.
stock_movements = Table(
    "stock_movements",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("presentation_id", Uuid, nullable=False, index=True),
    Column("kind", String(12), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("delta", Integer, nullable=False),
    Column("on_hand_after", Integer, nullable=False),
    Column("reason", String(200), nullable=False),
    Column("actor_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_stock_movements_presentation_created", "presentation_id", "created_at"),
    schema=SCHEMA,
)

# A single row (id 1): the global settings.
settings = Table(
    "settings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("low_stock_threshold", Integer, nullable=False),
    schema=SCHEMA,
)
