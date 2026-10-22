"""payments checkouts

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS payments")
    op.create_table(
        "checkouts",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("preference_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_url", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("order_id", name=op.f("pk_checkouts")),
        schema="payments",
    )


def downgrade() -> None:
    op.drop_table("checkouts", schema="payments")
    op.execute("DROP SCHEMA payments")
