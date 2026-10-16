from sqlalchemy import ARRAY, Boolean, Column, Integer, String, Table

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
