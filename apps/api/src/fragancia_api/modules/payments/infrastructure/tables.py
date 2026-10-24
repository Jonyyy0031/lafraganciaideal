from sqlalchemy import BigInteger, Column, DateTime, Index, String, Table
from sqlalchemy.types import Uuid

from fragancia_api.shared.infrastructure.tables import metadata

SCHEMA = "payments"

# One Mercado Pago preference per order. The order id is not a foreign key: orders owns its
# schema.
checkouts = Table(
    "checkouts",
    metadata,
    Column("order_id", Uuid, primary_key=True),
    Column("preference_id", String(64), nullable=False),
    Column("redirect_url", String(500), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

# One row per Mercado Pago payment, as last fetched. `order_id` is the payment's
# `external_reference` (no foreign key, as above); `outcome` is set once, when an approved
# payment is applied to its order.
payments = Table(
    "payments",
    metadata,
    Column("payment_id", String(32), primary_key=True),
    Column("order_id", Uuid, nullable=True),
    Column("status", String(32), nullable=False),
    Column("status_detail", String(100), nullable=True),
    Column("amount_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("payment_type", String(32), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("outcome", String(20), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_payments_order_id", "order_id"),
    schema=SCHEMA,
)
