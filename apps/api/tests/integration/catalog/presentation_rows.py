"""Direct catalog rows for tests of modules that read presentations through the facade."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfumes,
    presentations,
)
from fragancia_api.shared.kernel import new_id

CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class CatalogRows:
    """Seeds brands, perfumes and presentations; remembers what it created for cleanup."""

    def __init__(self, container: Container) -> None:
        self._container = container
        self._brands: dict[str, UUID] = {}
        self._family = new_id()
        self._concentration = new_id()
        self._ready = False

    async def _ensure_refs(self) -> None:
        if self._ready:
            return
        async with self._container.database.engine.begin() as connection:
            await connection.execute(
                insert(olfactory_families).values(
                    id=self._family,
                    name="Zz Rows Family",
                    slug="zz-rows-family",
                    is_active=True,
                    created_at=CREATED,
                )
            )
            await connection.execute(
                insert(concentrations).values(
                    id=self._concentration,
                    name="Zz Rows Eau",
                    slug="zz-rows-eau",
                    abbreviation="ZRE",
                    abbreviation_slug="zre",
                    is_active=True,
                    created_at=CREATED,
                )
            )
        self._ready = True

    async def _brand(self, name: str, *, active: bool) -> UUID:
        key = f"{name}|{active}"
        if key not in self._brands:
            brand_id = new_id()
            async with self._container.database.engine.begin() as connection:
                await connection.execute(
                    insert(brands).values(
                        id=brand_id,
                        name=name,
                        slug=f"{slugify(name)}-{brand_id}",
                        is_active=active,
                        created_at=CREATED,
                    )
                )
            self._brands[key] = brand_id
        return self._brands[key]

    async def perfume(
        self,
        name: str = "Eros",
        *,
        brand: str = "Zz Brand",
        brand_active: bool = True,
        archived: bool = False,
        sizes: tuple[dict[str, Any], ...] = ({},),
    ) -> tuple[UUID, list[UUID]]:
        """Insert a perfume and one presentation per dict (each overrides the defaults of one).
        Returns the perfume id and the presentation ids in order."""
        await self._ensure_refs()
        brand_id = await self._brand(brand, active=brand_active)
        perfume_id = new_id()
        values = {
            "id": perfume_id,
            "brand_id": brand_id,
            "concentration_id": self._concentration,
            "family_id": self._family,
            "name": name,
            "name_slug": slugify(name),
            "slug": f"{slugify(name)}-{perfume_id}",
            "gender": "men",
            "description": "",
            "top_notes": [],
            "heart_notes": [],
            "base_notes": [],
            "is_published": True,
            "first_published_at": CREATED,
            "is_archived": archived,
            "created_at": CREATED,
            "updated_at": CREATED,
        }
        ids: list[UUID] = []
        async with self._container.database.engine.begin() as connection:
            await connection.execute(insert(perfumes).values(values))
            for position, size in enumerate(sizes):
                presentation_id = new_id()
                ids.append(presentation_id)
                await connection.execute(
                    insert(presentations).values(
                        {
                            "id": presentation_id,
                            "perfume_id": perfume_id,
                            "ml": 100 + position,
                            "price_cents": 250_000,
                            "availability": "in_stock",
                            "is_active": True,
                            "created_at": CREATED,
                            **size,
                        }
                    )
                )
        return perfume_id, ids

    async def cleanup(self) -> None:
        async with self._container.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
            if self._brands:
                await connection.execute(
                    delete(brands).where(brands.c.id.in_(list(self._brands.values())))
                )
            if self._ready:
                await connection.execute(
                    delete(olfactory_families).where(olfactory_families.c.id == self._family)
                )
                await connection.execute(
                    delete(concentrations).where(concentrations.c.id == self._concentration)
                )


@pytest.fixture
async def catalog_rows(container: Container) -> AsyncIterator[CatalogRows]:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
    rows = CatalogRows(container)
    yield rows
    await rows.cleanup()
