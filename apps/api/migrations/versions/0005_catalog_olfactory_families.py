"""catalog olfactory families

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Starting list; the owner adds the rest from the back office. Slugs written literally.
_SEED = [
    ("Amaderada", "amaderada"),
    ("Floral", "floral"),
    ("Oriental", "oriental"),
    ("Cítrica", "citrica"),
    ("Aromática", "aromatica"),
    ("Gourmand", "gourmand"),
    ("Acuática", "acuatica"),
    ("Chipre", "chipre"),
    ("Fougère", "fougere"),
]


def upgrade() -> None:
    op.create_table(
        "olfactory_families",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_olfactory_families")),
        sa.UniqueConstraint("slug", name=op.f("uq_olfactory_families_slug")),
        schema="catalog",
    )
    families = sa.table(
        "olfactory_families",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        schema="catalog",
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        families,
        [
            {
                "id": uuid.uuid7(),
                "name": name,
                "slug": slug,
                "is_active": True,
                "created_at": now,
            }
            for name, slug in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("olfactory_families", schema="catalog")
