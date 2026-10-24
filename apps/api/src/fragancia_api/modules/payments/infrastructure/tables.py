from sqlalchemy import Column, DateTime, String, Table
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
