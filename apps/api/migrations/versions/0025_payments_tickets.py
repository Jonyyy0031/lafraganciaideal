"""payments tickets

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("payment_method", sa.String(length=32), nullable=True),
        schema="payments",
    )
    op.add_column(
        "payments",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="payments",
    )
    op.add_column(
        "payments",
        sa.Column("ticket_url", sa.String(length=500), nullable=True),
        schema="payments",
    )
    op.add_column(
        "payments",
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        schema="payments",
    )


def downgrade() -> None:
    op.drop_column("payments", "held_at", schema="payments")
    op.drop_column("payments", "ticket_url", schema="payments")
    op.drop_column("payments", "expires_at", schema="payments")
    op.drop_column("payments", "payment_method", schema="payments")
