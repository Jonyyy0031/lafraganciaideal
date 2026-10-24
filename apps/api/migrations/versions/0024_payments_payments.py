"""payments payments

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("payment_id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_detail", sa.String(length=100), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_type", sa.String(length=32), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("payment_id", name=op.f("pk_payments")),
        schema="payments",
    )
    op.create_index(
        "ix_payments_order_id", "payments", ["order_id"], unique=False, schema="payments"
    )


def downgrade() -> None:
    op.drop_index("ix_payments_order_id", table_name="payments", schema="payments")
    op.drop_table("payments", schema="payments")
