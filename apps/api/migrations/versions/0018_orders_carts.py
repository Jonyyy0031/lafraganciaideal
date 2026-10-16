"""orders carts

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "carts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carts")),
        sa.UniqueConstraint("token_digest", name=op.f("uq_carts_token_digest")),
        schema="orders",
    )
    op.create_index(
        op.f("ix_carts_updated_at"), "carts", ["updated_at"], unique=False, schema="orders"
    )
    op.create_table(
        "cart_lines",
        sa.Column("cart_id", sa.Uuid(), nullable=False),
        sa.Column("presentation_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("seen_price_cents", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity BETWEEN 1 AND 10", name=op.f("ck_cart_lines_quantity_range")),
        sa.ForeignKeyConstraint(
            ["cart_id"],
            ["orders.carts.id"],
            name=op.f("fk_cart_lines_cart_id_carts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("cart_id", "presentation_id", name=op.f("pk_cart_lines")),
        schema="orders",
    )


def downgrade() -> None:
    op.drop_table("cart_lines", schema="orders")
    op.drop_index(op.f("ix_carts_updated_at"), table_name="carts", schema="orders")
    op.drop_table("carts", schema="orders")
