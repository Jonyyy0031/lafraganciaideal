"""orders lifecycle

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=False),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("carrier", sa.String(length=60), nullable=True),
        sa.Column("tracking_number", sa.String(length=60), nullable=True),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.orders.id"],
            name=op.f("fk_order_events_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_events")),
        schema="orders",
    )
    op.create_index(
        "ix_order_events_order_id", "order_events", ["order_id"], unique=False, schema="orders"
    )
    op.create_table(
        "order_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("text", sa.String(length=1000), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.orders.id"],
            name=op.f("fk_order_notes_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_notes")),
        schema="orders",
    )
    op.create_index(
        "ix_order_notes_order_id", "order_notes", ["order_id"], unique=False, schema="orders"
    )
    op.add_column(
        "orders", sa.Column("cancel_reason", sa.String(length=300), nullable=True), schema="orders"
    )
    op.add_column(
        "orders", sa.Column("refund_status", sa.String(length=10), nullable=True), schema="orders"
    )
    op.add_column(
        "orders",
        sa.Column("shipping_carrier", sa.String(length=60), nullable=True),
        schema="orders",
    )
    op.add_column(
        "orders",
        sa.Column("tracking_number", sa.String(length=60), nullable=True),
        schema="orders",
    )
    op.create_check_constraint(
        op.f("ck_orders_status"),
        "orders",
        "status IN ('pending_payment', 'paid', 'preparing', 'shipped', 'ready_for_pickup', "
        "'delivered', 'cancelled')",
        schema="orders",
    )
    op.create_check_constraint(
        op.f("ck_orders_refund_status"),
        "orders",
        "refund_status IN ('pending', 'done')",
        schema="orders",
    )
    op.create_index(
        "ix_orders_payment_due_unpaid",
        "orders",
        ["payment_due_at"],
        unique=False,
        schema="orders",
        postgresql_where=sa.text("status = 'pending_payment'"),
    )
    op.create_index("ix_orders_status", "orders", ["status"], unique=False, schema="orders")


def downgrade() -> None:
    op.drop_index("ix_orders_status", table_name="orders", schema="orders")
    op.drop_index(
        "ix_orders_payment_due_unpaid",
        table_name="orders",
        schema="orders",
        postgresql_where=sa.text("status = 'pending_payment'"),
    )
    op.drop_constraint(op.f("ck_orders_refund_status"), "orders", schema="orders", type_="check")
    op.drop_constraint(op.f("ck_orders_status"), "orders", schema="orders", type_="check")
    op.drop_column("orders", "tracking_number", schema="orders")
    op.drop_column("orders", "shipping_carrier", schema="orders")
    op.drop_column("orders", "refund_status", schema="orders")
    op.drop_column("orders", "cancel_reason", schema="orders")
    op.drop_index("ix_order_notes_order_id", table_name="order_notes", schema="orders")
    op.drop_table("order_notes", schema="orders")
    op.drop_index("ix_order_events_order_id", table_name="order_events", schema="orders")
    op.drop_table("order_events", schema="orders")
