"""catalog perfume slug history and unaccent

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09 16:41:29.530767
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.create_table(
        "perfume_slug_history",
        sa.Column("slug", sa.String(length=240), nullable=False),
        sa.Column("perfume_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["perfume_id"],
            ["catalog.perfumes.id"],
            name=op.f("fk_perfume_slug_history_perfume_id_perfumes"),
        ),
        sa.PrimaryKeyConstraint("slug", name=op.f("pk_perfume_slug_history")),
        schema="catalog",
    )
    op.create_index(
        op.f("ix_perfume_slug_history_perfume_id"),
        "perfume_slug_history",
        ["perfume_id"],
        unique=False,
        schema="catalog",
    )


def downgrade() -> None:
    # The unaccent extension is left installed: dropping it is not reversible-safe if anything
    # else starts using it, and leaving it is harmless.
    op.drop_index(
        op.f("ix_perfume_slug_history_perfume_id"),
        table_name="perfume_slug_history",
        schema="catalog",
    )
    op.drop_table("perfume_slug_history", schema="catalog")
