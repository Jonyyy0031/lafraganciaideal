"""catalog concentrations

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Starting list; the owner edits it from the back office. Slugs written literally.
_SEED = [
    ("Eau de Cologne", "eau-de-cologne", "EDC", "edc"),
    ("Eau de Toilette", "eau-de-toilette", "EDT", "edt"),
    ("Eau de Parfum", "eau-de-parfum", "EDP", "edp"),
    ("Parfum", "parfum", "Parfum", "parfum"),
    ("Extrait de Parfum", "extrait-de-parfum", "Extrait", "extrait"),
]


def upgrade() -> None:
    op.create_table(
        "concentrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("abbreviation", sa.String(length=12), nullable=False),
        sa.Column("abbreviation_slug", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_concentrations")),
        sa.UniqueConstraint("abbreviation_slug", name=op.f("uq_concentrations_abbreviation_slug")),
        sa.UniqueConstraint("slug", name=op.f("uq_concentrations_slug")),
        schema="catalog",
    )
    concentrations = sa.table(
        "concentrations",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("abbreviation", sa.String()),
        sa.column("abbreviation_slug", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        schema="catalog",
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        concentrations,
        [
            {
                "id": uuid.uuid7(),
                "name": name,
                "slug": slug,
                "abbreviation": abbreviation,
                "abbreviation_slug": abbreviation_slug,
                "is_active": True,
                "created_at": now,
            }
            for name, slug, abbreviation, abbreviation_slug in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("concentrations", schema="catalog")
