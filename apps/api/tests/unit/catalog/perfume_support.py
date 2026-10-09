"""Builders shared by the perfume tests (domain, application and http layers)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fragancia_api.modules.catalog.contracts import PerfumeRequest, PresentationRequest
from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
from fragancia_api.modules.catalog.domain.concentration import (
    Abbreviation,
    Concentration,
    ConcentrationName,
)
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName, OlfactoryFamily
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    Perfume,
    PerfumeName,
    Price,
    perfume_slug,
)
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryBrands,
    InMemoryConcentrations,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_perfumes import InMemoryPerfumes
from fragancia_api.shared.kernel import Err, Ok, Result

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def unwrap_ok[T](result: Result[T, Any]) -> T:
    assert isinstance(result, Ok), result
    return result.value


def error_code(result: Result[Any, Any]) -> str:
    assert isinstance(result, Err), result
    return str(result.error.code)


def error_details(result: Result[Any, Any]) -> object:
    assert isinstance(result, Err), result
    return result.error.details


def make_brand(name: str = "Versace", *, active: bool = True) -> Brand:
    brand = Brand.create(unwrap_ok(BrandName.create(name)), created_at=NOW)
    brand.is_active = active
    return brand


def make_family(name: str = "Aromática", *, active: bool = True) -> OlfactoryFamily:
    family = OlfactoryFamily.create(unwrap_ok(FamilyName.create(name)), created_at=NOW)
    family.is_active = active
    return family


def make_concentration(
    name: str = "Eau de Toilette", abbreviation: str = "EDT", *, active: bool = True
) -> Concentration:
    concentration = Concentration.create(
        unwrap_ok(ConcentrationName.create(name)),
        unwrap_ok(Abbreviation.create(abbreviation)),
        created_at=NOW,
    )
    concentration.is_active = active
    return concentration


def make_perfume(
    brand: Brand,
    family: OlfactoryFamily,
    concentration: Concentration,
    *,
    name: str = "Eros",
) -> Perfume:
    perfume_name = unwrap_ok(PerfumeName.create(name))
    return Perfume.create(
        brand_id=brand.id,
        concentration_id=concentration.id,
        family_id=family.id,
        name=perfume_name,
        slug=perfume_slug(brand.name.value, perfume_name, concentration.abbreviation.value),
        gender=Gender.MEN,
        description=unwrap_ok(Description.create("")),
        notes=unwrap_ok(Notes.create([], [], [])),
        now=NOW,
    )


def add_presentation(perfume: Perfume, ml: int = 100, price_cents: int = 250_000) -> UUID:
    return unwrap_ok(
        perfume.add_presentation(
            unwrap_ok(Ml.create(ml)),
            unwrap_ok(Price.create(price_cents)),
            None,
            unwrap_ok(Availability.create("in_stock", None, None)),
            created_at=NOW,
        )
    )


@dataclass
class World:
    """A brand, family and concentration, all active, plus the stores around them."""

    brand: Brand = field(default_factory=make_brand)
    family: OlfactoryFamily = field(default_factory=make_family)
    concentration: Concentration = field(default_factory=make_concentration)

    def __post_init__(self) -> None:
        self.brands = InMemoryBrands(self.brand)
        self.families = InMemoryOlfactoryFamilies(self.family)
        self.concentrations = InMemoryConcentrations(self.concentration)
        self.perfumes = InMemoryPerfumes(self.brands, self.families, self.concentrations)

    def seed(self, perfume: Perfume) -> Perfume:
        self.perfumes.by_id[perfume.id] = perfume
        return perfume

    def request(self, **overrides: object) -> PerfumeRequest:
        values: dict[str, object] = {
            "brand_id": self.brand.id,
            "concentration_id": self.concentration.id,
            "family_id": self.family.id,
            "name": "Eros",
            "gender": "men",
        }
        values.update(overrides)
        return PerfumeRequest.model_validate(values)


def presentation_request(**overrides: object) -> PresentationRequest:
    values: dict[str, object] = {"ml": 100, "price_cents": 250_000, "availability": "in_stock"}
    values.update(overrides)
    return PresentationRequest.model_validate(values)
