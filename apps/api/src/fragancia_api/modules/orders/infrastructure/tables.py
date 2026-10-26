from sqlalchemy import (
    ARRAY,
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
    # Digest of the cart cookie the order was placed from; a retried checkout finds its order
    # through it. Nullable for orders stored before migration 0020.
    Column("cart_token_digest", String(64), nullable=True, index=True),
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
    Column("cancel_reason", String(300), nullable=True),
    Column("refund_status", String(10), nullable=True),
    Column("shipping_carrier", String(60), nullable=True),
    Column("tracking_number", String(60), nullable=True),
    CheckConstraint("delivery_method IN ('home', 'local', 'pickup')", name="delivery_method"),
    CheckConstraint("total_cents = subtotal_cents + delivery_cents", name="total_sum"),
    CheckConstraint(
        "status IN ('pending_payment', 'paid', 'preparing', 'shipped', 'ready_for_pickup', "
        "'delivered', 'cancelled')",
        name="status",
    ),
    CheckConstraint("refund_status IN ('pending', 'done')", name="refund_status"),
    Index("ix_orders_status", "status"),
    # The overdue job's scan: only unpaid orders.
    Index(
        "ix_orders_payment_due_unpaid",
        "payment_due_at",
        postgresql_where=text("status = 'pending_payment'"),
    ),
    schema=SCHEMA,
)

# One row per move of an order, oldest first when read back. `actor` is an admin user id,
# `system`, `customer` or `payments`; no foreign key because identity owns users.
order_events = Table(
    "order_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("order_id", Uuid, ForeignKey(orders.c.id, ondelete="CASCADE"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("from_status", String(30), nullable=False),
    Column("to_status", String(30), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("note", String(300), nullable=True),
    Column("carrier", String(60), nullable=True),
    Column("tracking_number", String(60), nullable=True),
    Index("ix_order_events_order_id", "order_id"),
    schema=SCHEMA,
)

order_notes = Table(
    "order_notes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("order_id", Uuid, ForeignKey(orders.c.id, ondelete="CASCADE"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("text", String(1000), nullable=False),
    Index("ix_order_notes_order_id", "order_id"),
    schema=SCHEMA,
)

# Fixed-window attempt counters of the customer's order lookup (decision 39): keys like
# `lookup-ip:<ip>` and `lookup-number:<n>`.
lookup_throttle = Table(
    "lookup_throttle",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("attempts", Integer, nullable=False),
    Column("window_started_at", DateTime(timezone=True), nullable=False),
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
