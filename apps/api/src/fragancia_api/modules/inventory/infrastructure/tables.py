from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    text,
)
from sqlalchemy.types import Uuid

from fragancia_api.shared.infrastructure.tables import metadata

SCHEMA = "inventory"

stock_items = Table(
    "stock_items",
    metadata,
    # The catalog's presentation id: not a foreign key, modules own their schemas.
    Column("presentation_id", Uuid, primary_key=True),
    Column("on_hand", Integer, nullable=False),
    # Units held by active reservations; changed under the item's row lock.
    Column("reserved", Integer, nullable=False, server_default=text("0")),
    Column("threshold_override", Integer, nullable=True),
    # The low-stock email went out and the item has not recovered since (re-arms above).
    Column("low_alerted", Boolean, nullable=False, server_default=text("false")),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("on_hand >= 0", name="on_hand_not_negative"),
    CheckConstraint("reserved >= 0", name="reserved_not_negative"),
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

reservations = Table(
    "reservations",
    metadata,
    Column("id", Uuid, primary_key=True),
    # The caller's reference (an order), unique: a retried reserve cannot hold twice.
    Column("reference", String(64), nullable=False, unique=True),
    Column("status", String(12), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    # The expiry job's scan: only active reservations.
    Index(
        "ix_reservations_active_expires",
        "expires_at",
        postgresql_where=text("status = 'active'"),
    ),
    schema=SCHEMA,
)

# The lines never change after the reservation is created.
reservation_lines = Table(
    "reservation_lines",
    metadata,
    Column("reservation_id", Uuid, ForeignKey(reservations.c.id), primary_key=True),
    # The catalog's presentation id: not a foreign key, modules own their schemas.
    Column("presentation_id", Uuid, primary_key=True, index=True),
    Column("quantity", Integer, nullable=False),
    CheckConstraint("quantity > 0", name="quantity_positive"),
    schema=SCHEMA,
)
