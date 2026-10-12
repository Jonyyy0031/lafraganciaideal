"""Read-only lookups for validating an import (outside any transaction: `Database.reader()`)."""

from collections.abc import Collection
from typing import Any

from sqlalchemy import Table, select, tuple_

from fragancia_api.modules.catalog.application.ports import (
    ExistingPerfume,
    PerfumeIdentity,
    ReferenceItem,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfume_photos,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import Database


class SqlImportReferences:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def brands(self) -> list[ReferenceItem]:
        return await self._items(brands)

    async def families(self) -> list[ReferenceItem]:
        return await self._items(olfactory_families)

    async def concentrations(self) -> list[ReferenceItem]:
        statement = select(
            concentrations.c.id,
            concentrations.c.slug,
            concentrations.c.abbreviation_slug,
            concentrations.c.name,
            concentrations.c.is_active,
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [
            ReferenceItem(
                id=row["id"],
                slug=row["slug"],
                alt_slug=row["abbreviation_slug"],
                name=row["name"],
                is_active=row["is_active"],
            )
            for row in rows
        ]

    async def _items(self, table: Table) -> list[ReferenceItem]:
        statement = select(table.c.id, table.c.slug, table.c.name, table.c.is_active)
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [
            ReferenceItem(
                id=row["id"],
                slug=row["slug"],
                alt_slug=None,
                name=row["name"],
                is_active=row["is_active"],
            )
            for row in rows
        ]

    async def existing_perfumes(
        self, identities: Collection[PerfumeIdentity]
    ) -> dict[PerfumeIdentity, ExistingPerfume]:
        if not identities:
            return {}
        identity = tuple_(perfumes.c.brand_id, perfumes.c.name_slug, perfumes.c.concentration_id)
        async with self._database.reader() as session:
            perfume_rows = (
                (
                    await session.execute(
                        select(
                            perfumes.c.id,
                            perfumes.c.brand_id,
                            perfumes.c.name_slug,
                            perfumes.c.concentration_id,
                            perfumes.c.family_id,
                            perfumes.c.is_archived,
                        ).where(identity.in_(list(identities)))
                    )
                )
                .mappings()
                .all()
            )
            ids = [row["id"] for row in perfume_rows]
            presentation_rows: list[Any] = []
            photo_rows: list[Any] = []
            if ids:
                presentation_rows = list(
                    (
                        await session.execute(
                            select(
                                presentations.c.perfume_id,
                                presentations.c.id,
                                presentations.c.ml,
                                presentations.c.is_active,
                            ).where(presentations.c.perfume_id.in_(ids))
                        )
                    )
                    .mappings()
                    .all()
                )
                photo_rows = list(
                    (
                        await session.execute(
                            select(
                                perfume_photos.c.perfume_id,
                                perfume_photos.c.id,
                                perfume_photos.c.position,
                                perfume_photos.c.source_url,
                            ).where(perfume_photos.c.perfume_id.in_(ids))
                        )
                    )
                    .mappings()
                    .all()
                )

        found: dict[PerfumeIdentity, ExistingPerfume] = {}
        by_id: dict[Any, ExistingPerfume] = {}
        for row in perfume_rows:
            existing = ExistingPerfume(
                id=row["id"],
                is_archived=row["is_archived"],
                family_id=row["family_id"],
                presentations={},
                photos={},
            )
            found[(row["brand_id"], row["name_slug"], row["concentration_id"])] = existing
            by_id[row["id"]] = existing
        for row in presentation_rows:
            by_id[row["perfume_id"]].presentations[row["ml"]] = (row["id"], row["is_active"])
        for row in photo_rows:
            by_id[row["perfume_id"]].photos[row["position"]] = (row["id"], row["source_url"])
        return found
