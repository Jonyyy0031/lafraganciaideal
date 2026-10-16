"""orders delivery settings

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "orders"')
    op.create_table(
        "delivery_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("home_enabled", sa.Boolean(), nullable=False),
        sa.Column("home_price_cents", sa.Integer(), nullable=False),
        sa.Column("home_free_from_cents", sa.Integer(), nullable=True),
        sa.Column("local_enabled", sa.Boolean(), nullable=False),
        sa.Column("local_price_cents", sa.Integer(), nullable=False),
        sa.Column("local_postal_codes", sa.ARRAY(sa.String(length=5)), nullable=False),
        sa.Column("pickup_enabled", sa.Boolean(), nullable=False),
        sa.Column("pickup_address", sa.String(length=300), nullable=False),
        sa.Column("pickup_hours", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_settings")),
        schema="orders",
    )
    # The default row: every method off, prices 0, no postal codes, no pickup point.
    op.execute(
        """
        INSERT INTO orders.delivery_settings (
            id, home_enabled, home_price_cents, home_free_from_cents,
            local_enabled, local_price_cents, local_postal_codes,
            pickup_enabled, pickup_address, pickup_hours
        ) VALUES (1, false, 0, NULL, false, 0, ARRAY[]::varchar(5)[], false, '', '')
        """
    )


def downgrade() -> None:
    op.drop_table("delivery_settings", schema="orders")
    op.execute('DROP SCHEMA IF EXISTS "orders"')
