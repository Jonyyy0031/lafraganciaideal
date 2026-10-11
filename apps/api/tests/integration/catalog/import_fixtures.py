"""Fixtures and helpers of the catalog import integration tests (plan 007): one brand, two
families (one archived) and one concentration in `fragancia_test`, plus seeding helpers."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import photo_keys
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfume_photos,
    perfumes,
    presentations,
)
from fragancia_api.shared.kernel import new_id

CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
BRAND_NAME = "Zz Imp Brand"
FAMILY_NAME = "Zz Imp Family"
OTHER_FAMILY_NAME = "Zz Imp Other Family"
OLD_FAMILY_NAME = "Zz Imp Old Family"
CONCENTRATION_NAME = "Zz Imp Concentration"
CONCENTRATION_ABBREVIATION = "ZIC"


@dataclass(frozen=True)
class World:
    brand: UUID
    family: UUID
    other_family: UUID
    old_family: UUID
    concentration: UUID


async def _clear(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.imports"))
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(delete(brands).where(brands.c.slug == slugify(BRAND_NAME)))
        await connection.execute(
            delete(olfactory_families).where(
                olfactory_families.c.slug.in_(
                    [slugify(n) for n in (FAMILY_NAME, OTHER_FAMILY_NAME, OLD_FAMILY_NAME)]
                )
            )
        )
        await connection.execute(
            delete(concentrations).where(concentrations.c.slug == slugify(CONCENTRATION_NAME))
        )


@pytest.fixture
async def world(container: Container) -> AsyncIterator[World]:
    """Empties the imports and perfumes around each test and owns its reference rows."""
    await _clear(container)
    ids = World(new_id(), new_id(), new_id(), new_id(), new_id())
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(brands),
            {
                "id": ids.brand,
                "name": BRAND_NAME,
                "slug": slugify(BRAND_NAME),
                "is_active": True,
                "created_at": CREATED,
            },
        )
        for family_id, name, active in (
            (ids.family, FAMILY_NAME, True),
            (ids.other_family, OTHER_FAMILY_NAME, True),
            (ids.old_family, OLD_FAMILY_NAME, False),
        ):
            await connection.execute(
                insert(olfactory_families),
                {
                    "id": family_id,
                    "name": name,
                    "slug": slugify(name),
                    "is_active": active,
                    "created_at": CREATED,
                },
            )
        await connection.execute(
            insert(concentrations),
            {
                "id": ids.concentration,
                "name": CONCENTRATION_NAME,
                "slug": slugify(CONCENTRATION_NAME),
                "abbreviation": CONCENTRATION_ABBREVIATION,
                "abbreviation_slug": slugify(CONCENTRATION_ABBREVIATION),
                "is_active": True,
                "created_at": CREATED,
            },
        )
    yield ids
    await _clear(container)


async def seed_perfume(
    container: Container,
    world: World,
    name: str,
    *,
    archived: bool = False,
    family: UUID | None = None,
    presentation_ml: tuple[int, ...] = (),
    archived_ml: tuple[int, ...] = (),
) -> UUID:
    perfume_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(perfumes).values(
                {
                    "id": perfume_id,
                    "brand_id": world.brand,
                    "concentration_id": world.concentration,
                    "family_id": family or world.family,
                    "name": name,
                    "name_slug": slugify(name),
                    "slug": f"{slugify(name)}-{perfume_id}",
                    "gender": "men",
                    "description": "",
                    "top_notes": [],
                    "heart_notes": [],
                    "base_notes": [],
                    "is_published": False,
                    "first_published_at": None,
                    "is_archived": archived,
                    "created_at": CREATED,
                    "updated_at": CREATED,
                }
            )
        )
        for ml, active in [(m, True) for m in presentation_ml] + [(m, False) for m in archived_ml]:
            await connection.execute(
                insert(presentations).values(
                    {
                        "id": new_id(),
                        "perfume_id": perfume_id,
                        "ml": ml,
                        "price_cents": 250_000,
                        "availability": "in_stock",
                        "is_active": active,
                        "created_at": CREATED,
                    }
                )
            )
    return perfume_id


async def seed_photo(
    container: Container,
    perfume_id: UUID,
    position: int,
    *,
    source_url: str | None = None,
) -> UUID:
    photo_id = new_id()
    card_key, detail_key = photo_keys(perfume_id, photo_id)
    values: dict[str, Any] = {
        "id": photo_id,
        "perfume_id": perfume_id,
        "position": position,
        "card_key": card_key,
        "detail_key": detail_key,
        "width": 1200,
        "height": 900,
        "created_at": CREATED + timedelta(minutes=position),
        "source_url": source_url,
    }
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(perfume_photos).values(values))
    return photo_id
