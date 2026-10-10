from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
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

concentrations = Table(
    "concentrations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(80), nullable=False),
    Column("slug", String(100), nullable=False, unique=True),
    Column("abbreviation", String(12), nullable=False),
    Column("abbreviation_slug", String(20), nullable=False, unique=True),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)

CONCENTRATION_SLUG_UNIQUE = "uq_concentrations_slug"
CONCENTRATION_ABBREVIATION_UNIQUE = "uq_concentrations_abbreviation_slug"

# Foreign keys stay inside the catalog schema (allowed within one module).
perfumes = Table(
    "perfumes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("brand_id", Uuid, ForeignKey(brands.c.id), nullable=False, index=True),
    Column("concentration_id", Uuid, ForeignKey(concentrations.c.id), nullable=False),
    Column("family_id", Uuid, ForeignKey(olfactory_families.c.id), nullable=False, index=True),
    Column("name", String(80), nullable=False),
    Column("name_slug", String(100), nullable=False),
    Column("slug", String(240), nullable=False, unique=True),
    Column("gender", String(10), nullable=False),
    Column("description", Text, nullable=False),
    Column("top_notes", ARRAY(String(40)), nullable=False),
    Column("heart_notes", ARRAY(String(40)), nullable=False),
    Column("base_notes", ARRAY(String(40)), nullable=False),
    Column("is_published", Boolean, nullable=False),
    Column("first_published_at", DateTime(timezone=True), nullable=True),
    Column("is_archived", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("brand_id", "name_slug", "concentration_id", name="uq_perfumes_identity"),
    schema=SCHEMA,
)

PERFUME_IDENTITY_UNIQUE = "uq_perfumes_identity"
PERFUME_SLUG_UNIQUE = "uq_perfumes_slug"

presentations = Table(
    "presentations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("perfume_id", Uuid, ForeignKey(perfumes.c.id), nullable=False, index=True),
    Column("ml", Integer, nullable=False),
    Column("price_cents", Integer, nullable=False),
    Column("sale_price_cents", Integer, nullable=True),
    Column("sale_starts_at", DateTime(timezone=True), nullable=True),
    Column("sale_ends_at", DateTime(timezone=True), nullable=True),
    Column("availability", String(20), nullable=False),
    Column("lead_time_min_days", Integer, nullable=True),
    Column("lead_time_max_days", Integer, nullable=True),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("perfume_id", "ml", name="uq_presentations_perfume_ml"),
    schema=SCHEMA,
)

PRESENTATION_ML_UNIQUE = "uq_presentations_perfume_ml"

# Slugs a perfume used to have; the latest perfume to drop a slug owns it.
perfume_slug_history = Table(
    "perfume_slug_history",
    metadata,
    Column("slug", String(240), primary_key=True),
    Column("perfume_id", Uuid, ForeignKey(perfumes.c.id), nullable=False, index=True),
    Column("retired_at", DateTime(timezone=True), nullable=False),
    schema=SCHEMA,
)
