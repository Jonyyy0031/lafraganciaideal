"""inventory reservations

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservations")),
        sa.UniqueConstraint("reference", name=op.f("uq_reservations_reference")),
        schema="inventory",
    )
    op.create_index(
        "ix_reservations_active_expires",
        "reservations",
        ["expires_at"],
        unique=False,
        schema="inventory",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "reservation_lines",
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("presentation_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_reservation_lines_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["inventory.reservations.id"],
            name=op.f("fk_reservation_lines_reservation_id_reservations"),
        ),
        sa.PrimaryKeyConstraint(
            "reservation_id", "presentation_id", name=op.f("pk_reservation_lines")
        ),
        schema="inventory",
    )
    op.create_index(
        op.f("ix_reservation_lines_presentation_id"),
        "reservation_lines",
        ["presentation_id"],
        unique=False,
        schema="inventory",
    )
    op.add_column(
        "stock_items",
        sa.Column("reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema="inventory",
    )
    # Autogenerate does not detect a check constraint added to an existing table.
    op.create_check_constraint(
        op.f("ck_stock_items_reserved_not_negative"),
        "stock_items",
        "reserved >= 0",
        schema="inventory",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_stock_items_reserved_not_negative"),
        "stock_items",
        type_="check",
        schema="inventory",
    )
    op.drop_column("stock_items", "reserved", schema="inventory")
    op.drop_index(
        op.f("ix_reservation_lines_presentation_id"),
        table_name="reservation_lines",
        schema="inventory",
    )
    op.drop_table("reservation_lines", schema="inventory")
    op.drop_index(
        "ix_reservations_active_expires",
        table_name="reservations",
        schema="inventory",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("reservations", schema="inventory")
