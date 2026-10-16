from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
)
from sqlalchemy.types import Uuid

from fragancia_api.shared.infrastructure.tables import metadata

SCHEMA = "orders"

# A single row (id 1): the delivery configuration.
delivery_settings = Table(
    "delivery_settings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("home_enabled", Boolean, nullable=False),
    Column("home_price_cents", Integer, nullable=False),
    Column("home_free_from_cents", Integer, nullable=True),
    Column("local_enabled", Boolean, nullable=False),
    Column("local_price_cents", Integer, nullable=False),
    Column("local_postal_codes", ARRAY(String(5)), nullable=False),
    Column("pickup_enabled", Boolean, nullable=False),
    Column("pickup_address", String(300), nullable=False),
    Column("pickup_hours", String(200), nullable=False),
    schema=SCHEMA,
)

carts = Table(
    "carts",
    metadata,
    Column("id", Uuid, primary_key=True),
    # SHA-256 hex digest of the cookie token; the token itself is never stored.
    Column("token_digest", String(64), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, index=True),
    schema=SCHEMA,
)

cart_lines = Table(
    "cart_lines",
    metadata,
    Column("cart_id", Uuid, ForeignKey(carts.c.id, ondelete="CASCADE"), primary_key=True),
    # No foreign key: the presentation belongs to the catalog schema.
    Column("presentation_id", Uuid, primary_key=True),
    Column("quantity", Integer, nullable=False),
    Column("seen_price_cents", Integer, nullable=False),
    Column("added_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("quantity BETWEEN 1 AND 10", name="quantity_range"),
    schema=SCHEMA,
)
