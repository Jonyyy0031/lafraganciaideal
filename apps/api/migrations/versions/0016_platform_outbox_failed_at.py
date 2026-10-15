"""platform outbox failed at

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.drop_index("ix_outbox_pending", table_name="outbox", schema="platform")
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["available_at"],
        schema="platform",
        postgresql_where=sa.text("published_at IS NULL AND failed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox", schema="platform")
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["available_at"],
        schema="platform",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_column("outbox", "failed_at", schema="platform")
