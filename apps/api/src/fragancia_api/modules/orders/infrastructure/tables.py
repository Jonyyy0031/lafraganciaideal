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

# A single row (id 1): the gapless order counter, updated in the placing transaction.
order_numbers = Table(
    "order_numbers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("last_number", Integer, nullable=False),
    schema=SCHEMA,
)

orders = Table(
    "orders",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("number", Integer, nullable=False, unique=True),
    Column("status", String(30), nullable=False),
    # SHA-256 hex digest of the private-link token; the token itself is never stored.
    Column("access_token_digest", String(64), nullable=False, unique=True),
    Column("customer_name", String(120), nullable=False),
    Column("customer_email", String(254), nullable=False, index=True),
    Column("customer_phone", String(10), nullable=False),
    Column("delivery_method", String(10), nullable=False),
    Column("delivery_cents", Integer, nullable=False),
    Column("address_street", String(120), nullable=True),
    Column("address_exterior_number", String(20), nullable=True),
    Column("address_interior_number", String(20), nullable=True),
    Column("address_neighborhood", String(120), nullable=True),
    Column("address_postal_code", String(5), nullable=True),
    Column("address_municipality", String(120), nullable=True),
    Column("address_state", String(3), nullable=True),
    Column("address_references", String(300), nullable=True),
    Column("pickup_address", String(300), nullable=True),
    Column("pickup_hours", String(200), nullable=True),
    Column("invoice_rfc", String(13), nullable=True),
    Column("invoice_business_name", String(254), nullable=True),
    Column("invoice_tax_regime", String(3), nullable=True),
    Column("invoice_cfdi_use", String(3), nullable=True),
    Column("invoice_postal_code", String(5), nullable=True),
    Column("subtotal_cents", Integer, nullable=False),
    Column("total_cents", Integer, nullable=False),
    Column("lead_time_max_days", Integer, nullable=True),
    # No foreign key: the reservation belongs to inventory's schema.
    Column("reservation_id", Uuid, nullable=True),
    Column("payment_due_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("delivery_method IN ('home', 'local', 'pickup')", name="delivery_method"),
    CheckConstraint("total_cents = subtotal_cents + delivery_cents", name="total_sum"),
    schema=SCHEMA,
)

order_lines = Table(
    "order_lines",
    metadata,
    Column("order_id", Uuid, ForeignKey(orders.c.id, ondelete="CASCADE"), primary_key=True),
    Column("position", Integer, primary_key=True, autoincrement=False),
    # No foreign keys: both belong to the catalog schema.
    Column("presentation_id", Uuid, nullable=False),
    Column("perfume_id", Uuid, nullable=False),
    Column("label", String(300), nullable=False),
    Column("ml", Integer, nullable=False),
    Column("availability", String(20), nullable=False),
    Column("lead_time_min_days", Integer, nullable=True),
    Column("lead_time_max_days", Integer, nullable=True),
    Column("quantity", Integer, nullable=False),
    Column("unit_price_cents", Integer, nullable=False),
    Column("regular_price_cents", Integer, nullable=False),
    Column("line_total_cents", Integer, nullable=False),
    CheckConstraint("quantity BETWEEN 1 AND 10", name="quantity_range"),
    CheckConstraint("line_total_cents = unit_price_cents * quantity", name="line_total"),
    schema=SCHEMA,
)
