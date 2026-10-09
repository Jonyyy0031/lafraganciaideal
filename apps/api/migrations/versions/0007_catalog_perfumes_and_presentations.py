"""catalog perfumes and presentations

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perfumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("concentration_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("name_slug", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=240), nullable=False),
        sa.Column("gender", sa.String(length=10), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("top_notes", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        sa.Column("heart_notes", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        sa.Column("base_notes", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["catalog.brands.id"], name=op.f("fk_perfumes_brand_id_brands")
        ),
        sa.ForeignKeyConstraint(
            ["concentration_id"],
            ["catalog.concentrations.id"],
            name=op.f("fk_perfumes_concentration_id_concentrations"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["catalog.olfactory_families.id"],
            name=op.f("fk_perfumes_family_id_olfactory_families"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_perfumes")),
        sa.UniqueConstraint(
            "brand_id", "name_slug", "concentration_id", name=op.f("uq_perfumes_identity")
        ),
        sa.UniqueConstraint("slug", name=op.f("uq_perfumes_slug")),
        schema="catalog",
    )
    op.create_index(
        op.f("ix_perfumes_brand_id"), "perfumes", ["brand_id"], unique=False, schema="catalog"
    )
    op.create_index(
        op.f("ix_perfumes_family_id"), "perfumes", ["family_id"], unique=False, schema="catalog"
    )
    op.create_table(
        "presentations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("perfume_id", sa.Uuid(), nullable=False),
        sa.Column("ml", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("sale_price_cents", sa.Integer(), nullable=True),
        sa.Column("sale_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sale_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("lead_time_min_days", sa.Integer(), nullable=True),
        sa.Column("lead_time_max_days", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["perfume_id"],
            ["catalog.perfumes.id"],
            name=op.f("fk_presentations_perfume_id_perfumes"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_presentations")),
        sa.UniqueConstraint("perfume_id", "ml", name=op.f("uq_presentations_perfume_ml")),
        schema="catalog",
    )
    op.create_index(
        op.f("ix_presentations_perfume_id"),
        "presentations",
        ["perfume_id"],
        unique=False,
        schema="catalog",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_presentations_perfume_id"), table_name="presentations", schema="catalog")
    op.drop_table("presentations", schema="catalog")
    op.drop_index(op.f("ix_perfumes_family_id"), table_name="perfumes", schema="catalog")
    op.drop_index(op.f("ix_perfumes_brand_id"), table_name="perfumes", schema="catalog")
    op.drop_table("perfumes", schema="catalog")
