"""orders cart token

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The digest of the cart cookie the order was placed from, so a retried checkout finds its
    # order (decision 31). Nullable: orders stored before this migration have none.
    op.add_column(
        "orders",
        sa.Column("cart_token_digest", sa.String(length=64), nullable=True),
        schema="orders",
    )
    op.create_index(
        op.f("ix_orders_cart_token_digest"),
        "orders",
        ["cart_token_digest"],
        unique=False,
        schema="orders",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_orders_cart_token_digest"), table_name="orders", schema="orders")
    op.drop_column("orders", "cart_token_digest", schema="orders")
