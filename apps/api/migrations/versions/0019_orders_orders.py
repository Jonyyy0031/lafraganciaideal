"""orders orders

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-16 16:21:16.852052
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_numbers",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_numbers")),
        schema="orders",
    )
    # The gapless counter: the first order takes 1001 (decision 30).
    op.execute("INSERT INTO orders.order_numbers (id, last_number) VALUES (1, 1000)")
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("access_token_digest", sa.String(length=64), nullable=False),
        sa.Column("customer_name", sa.String(length=120), nullable=False),
        sa.Column("customer_email", sa.String(length=254), nullable=False),
        sa.Column("customer_phone", sa.String(length=10), nullable=False),
        sa.Column("delivery_method", sa.String(length=10), nullable=False),
        sa.Column("delivery_cents", sa.Integer(), nullable=False),
        sa.Column("address_street", sa.String(length=120), nullable=True),
        sa.Column("address_exterior_number", sa.String(length=20), nullable=True),
        sa.Column("address_interior_number", sa.String(length=20), nullable=True),
        sa.Column("address_neighborhood", sa.String(length=120), nullable=True),
        sa.Column("address_postal_code", sa.String(length=5), nullable=True),
        sa.Column("address_municipality", sa.String(length=120), nullable=True),
        sa.Column("address_state", sa.String(length=3), nullable=True),
        sa.Column("address_references", sa.String(length=300), nullable=True),
        sa.Column("pickup_address", sa.String(length=300), nullable=True),
        sa.Column("pickup_hours", sa.String(length=200), nullable=True),
        sa.Column("invoice_rfc", sa.String(length=13), nullable=True),
        sa.Column("invoice_business_name", sa.String(length=254), nullable=True),
        sa.Column("invoice_tax_regime", sa.String(length=3), nullable=True),
        sa.Column("invoice_cfdi_use", sa.String(length=3), nullable=True),
        sa.Column("invoice_postal_code", sa.String(length=5), nullable=True),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("lead_time_max_days", sa.Integer(), nullable=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("payment_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "delivery_method IN ('home', 'local', 'pickup')", name=op.f("ck_orders_delivery_method")
        ),
        sa.CheckConstraint(
            "total_cents = subtotal_cents + delivery_cents", name=op.f("ck_orders_total_sum")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("access_token_digest", name=op.f("uq_orders_access_token_digest")),
        sa.UniqueConstraint("number", name=op.f("uq_orders_number")),
        schema="orders",
    )
    op.create_index(
        op.f("ix_orders_customer_email"),
        "orders",
        ["customer_email"],
        unique=False,
        schema="orders",
    )
    op.create_table(
        "order_lines",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("presentation_id", sa.Uuid(), nullable=False),
        sa.Column("perfume_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("ml", sa.Integer(), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("lead_time_min_days", sa.Integer(), nullable=True),
        sa.Column("lead_time_max_days", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("regular_price_cents", sa.Integer(), nullable=False),
        sa.Column("line_total_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "line_total_cents = unit_price_cents * quantity", name=op.f("ck_order_lines_line_total")
        ),
        sa.CheckConstraint("quantity BETWEEN 1 AND 10", name=op.f("ck_order_lines_quantity_range")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.orders.id"],
            name=op.f("fk_order_lines_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("order_id", "position", name=op.f("pk_order_lines")),
        schema="orders",
    )


def downgrade() -> None:
    op.drop_table("order_lines", schema="orders")
    op.drop_index(op.f("ix_orders_customer_email"), table_name="orders", schema="orders")
    op.drop_table("orders", schema="orders")
    op.drop_table("order_numbers", schema="orders")
