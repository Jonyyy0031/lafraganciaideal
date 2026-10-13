"""inventory stock

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "inventory"')
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settings")),
        schema="inventory",
    )
    op.create_table(
        "stock_items",
        sa.Column("presentation_id", sa.Uuid(), nullable=False),
        sa.Column("on_hand", sa.Integer(), nullable=False),
        sa.Column("threshold_override", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("on_hand >= 0", name=op.f("ck_stock_items_on_hand_not_negative")),
        sa.PrimaryKeyConstraint("presentation_id", name=op.f("pk_stock_items")),
        schema="inventory",
    )
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("presentation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("on_hand_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_movements")),
        schema="inventory",
    )
    op.create_index(
        "ix_stock_movements_presentation_created",
        "stock_movements",
        ["presentation_id", "created_at"],
        unique=False,
        schema="inventory",
    )
    op.create_index(
        op.f("ix_stock_movements_presentation_id"),
        "stock_movements",
        ["presentation_id"],
        unique=False,
        schema="inventory",
    )
    # The single settings row: the global low-stock threshold starts at 3.
    settings = sa.table(
        "settings",
        sa.column("id", sa.Integer()),
        sa.column("low_stock_threshold", sa.Integer()),
        schema="inventory",
    )
    op.bulk_insert(settings, [{"id": 1, "low_stock_threshold": 3}])


def downgrade() -> None:
    op.drop_index(
        op.f("ix_stock_movements_presentation_id"),
        table_name="stock_movements",
        schema="inventory",
    )
    op.drop_index(
        "ix_stock_movements_presentation_created",
        table_name="stock_movements",
        schema="inventory",
    )
    op.drop_table("stock_movements", schema="inventory")
    op.drop_table("stock_items", schema="inventory")
    op.drop_table("settings", schema="inventory")
    op.execute('DROP SCHEMA IF EXISTS "inventory"')
