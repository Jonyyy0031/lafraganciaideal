"""inventory low stock alert

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stock_items",
        sa.Column("low_alerted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema="inventory",
    )
    # One-time backfill so the items that are already low send no burst of emails on deploy
    # (decision 34). Same rule as `StockItem.track_low`: units on hand against the effective
    # threshold.
    op.execute(
        """
        UPDATE inventory.stock_items AS si
        SET low_alerted = true
        WHERE si.on_hand <= coalesce(
            si.threshold_override,
            (SELECT s.low_stock_threshold FROM inventory.settings AS s LIMIT 1),
            3
        )
        """
    )


def downgrade() -> None:
    op.drop_column("stock_items", "low_alerted", schema="inventory")
