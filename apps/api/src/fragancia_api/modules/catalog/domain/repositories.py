from typing import Protocol
from uuid import UUID

from fragancia_api.modules.catalog.domain.brand import Brand
from fragancia_api.modules.catalog.domain.concentration import Concentration
from fragancia_api.modules.catalog.domain.errors import (
    BrandAlreadyExists,
    ConcentrationAbbreviationTaken,
    ConcentrationAlreadyExists,
    FamilyAlreadyExists,
)
from fragancia_api.modules.catalog.domain.olfactory_family import OlfactoryFamily
from fragancia_api.shared.kernel import Result


class BrandRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see BrandQueries."""

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        """Whether a brand has the slug, ignoring the brand with `except_id`."""
        ...

    async def add(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        """Err when another brand already has the slug (also under concurrent inserts)."""
        ...

    async def get_for_update(self, brand_id: UUID) -> Brand | None:
        """The brand, with its row locked until the transaction ends."""
        ...

    async def save(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
        """Persist name, slug and active flag. Err when another brand has the slug (also under
        concurrent renames)."""
        ...


class OlfactoryFamilyRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see the queries."""

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        """Whether a family has the slug, ignoring the family with `except_id`."""
        ...

    async def add(self, family: OlfactoryFamily) -> Result[None, FamilyAlreadyExists]:
        """Err when another family already has the slug (also under concurrent inserts)."""
        ...

    async def get_for_update(self, family_id: UUID) -> OlfactoryFamily | None:
        """The family, with its row locked until the transaction ends."""
        ...

    async def save(self, family: OlfactoryFamily) -> Result[None, FamilyAlreadyExists]:
        """Persist name, slug and active flag. Err when another family has the slug (also under
        concurrent renames)."""
        ...


class ConcentrationRepository(Protocol):
    """Write side. Joins the active transaction. No "for a screen" methods: see the queries."""

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        """Whether a concentration has the name slug, ignoring the one with `except_id`."""
        ...

    async def exists_with_abbreviation(
        self, abbreviation_slug: str, *, except_id: UUID | None = None
    ) -> bool:
        """Whether a concentration has the abbreviation slug, ignoring `except_id`."""
        ...

    async def add(
        self, concentration: Concentration
    ) -> Result[None, ConcentrationAlreadyExists | ConcentrationAbbreviationTaken]:
        """Err when another concentration has the name or abbreviation slug (also under
        concurrent inserts)."""
        ...

    async def get_for_update(self, concentration_id: UUID) -> Concentration | None:
        """The concentration, with its row locked until the transaction ends."""
        ...

    async def save(
        self, concentration: Concentration
    ) -> Result[None, ConcentrationAlreadyExists | ConcentrationAbbreviationTaken]:
        """Persist name, slug, abbreviation, abbreviation slug and active flag. Err when another
        concentration has either slug (also under concurrent updates)."""
        ...
