"""catalog presentation stock copy

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "presentations",
        sa.Column("stock_available", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema="catalog",
    )
    op.add_column(
        "presentations",
        sa.Column("stock_threshold", sa.Integer(), server_default=sa.text("3"), nullable=False),
        schema="catalog",
    )
    op.add_column(
        "presentations",
        sa.Column("stock_as_of", sa.DateTime(timezone=True), nullable=True),
        schema="catalog",
    )
    # One-time backfill of the copy from the inventory tables. This is a platform migration, the
    # only place that reads across schemas: module code never does. From here on the
    # `inventory.stock.changed` events keep the copy current.
    op.execute(
        """
        UPDATE catalog.presentations AS p
        SET stock_available = si.on_hand,
            stock_threshold = coalesce(
                si.threshold_override,
                (SELECT s.low_stock_threshold FROM inventory.settings AS s LIMIT 1),
                3
            ),
            stock_as_of = si.updated_at
        FROM inventory.stock_items AS si
        WHERE si.presentation_id = p.id
        """
    )


def downgrade() -> None:
    op.drop_column("presentations", "stock_as_of", schema="catalog")
    op.drop_column("presentations", "stock_threshold", schema="catalog")
    op.drop_column("presentations", "stock_available", schema="catalog")
