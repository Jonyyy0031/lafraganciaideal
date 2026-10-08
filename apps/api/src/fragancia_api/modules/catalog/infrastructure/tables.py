from sqlalchemy import Boolean, Column, DateTime, String, Table
from sqlalchemy.types import Uuid

from fragancia_api.shared.infrastructure.tables import metadata

SCHEMA = "catalog"

brands = Table(
    "brands",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(80), nullable=False),
    Column("slug", String(100), nullable=False, unique=True),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

BRAND_SLUG_UNIQUE = "uq_brands_slug"

olfactory_families = Table(
    "olfactory_families",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(80), nullable=False),
    Column("slug", String(100), nullable=False, unique=True),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

FAMILY_SLUG_UNIQUE = "uq_olfactory_families_slug"
