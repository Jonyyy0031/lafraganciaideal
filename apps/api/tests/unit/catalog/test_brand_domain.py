from datetime import UTC, datetime

import pytest

from fragancia_api.modules.catalog.domain.brand import Brand, BrandCreated, BrandName, slugify
from fragancia_api.modules.catalog.domain.errors import BrandNameInvalid
from fragancia_api.shared.kernel import Err, Ok


def name(raw: str) -> BrandName:
    result = BrandName.create(raw)
    assert isinstance(result, Ok), result
    return result.value


@pytest.mark.parametrize(
    ("raw", "value", "slug"),
    [
        ("  Maison   Margiela ", "Maison Margiela", "maison-margiela"),
        ("maison margiéla", "maison margiéla", "maison-margiela"),
        ("Dolce & Gabbana", "Dolce & Gabbana", "dolce-gabbana"),
        ("Carolina Herrera", "Carolina Herrera", "carolina-herrera"),
        ("4711", "4711", "4711"),
        ("YSL", "YSL", "ysl"),
    ],
)
def test_names_are_normalized_and_slugged(raw: str, value: str, slug: str) -> None:
    brand_name = name(raw)
    assert (brand_name.value, brand_name.slug) == (value, slug)


@pytest.mark.parametrize("raw", ["", " ", "x", "  y  ", "!!", "—·—", "a" * 81])
def test_invalid_names(raw: str) -> None:
    result = BrandName.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, BrandNameInvalid)
    assert result.error.code == "CATALOG_BRAND_NAME_INVALID"
    assert result.error.details == {"min": 2, "max": 80}


def test_length_limits_are_inclusive() -> None:
    assert isinstance(BrandName.create("ab"), Ok)
    assert isinstance(BrandName.create("a" * 80), Ok)


def test_slugify_strips_accents_and_symbols() -> None:
    assert slugify("  Éclat d'Arpège — Lanvin ") == "eclat-d-arpege-lanvin"


def test_new_brands_are_active_and_record_brand_created() -> None:
    created_at = datetime(2026, 10, 2, tzinfo=UTC)
    brand = Brand.create(name("Maison Margiela"), created_at=created_at)

    assert brand.is_active
    assert brand.created_at == created_at
    [event] = brand.pull_events()
    assert isinstance(event, BrandCreated)
    assert event.name == "catalog.brand.created"
    assert event.payload() == {
        "brand_id": str(brand.id),
        "brand_name": "Maison Margiela",
        "slug": "maison-margiela",
    }
