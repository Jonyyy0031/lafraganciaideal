from datetime import UTC, datetime

import pytest

from fragancia_api.modules.catalog.domain import brand as brand_module
from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
from fragancia_api.modules.catalog.domain.errors import FamilyNameInvalid
from fragancia_api.modules.catalog.domain.naming import clean_name, slugify
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName, OlfactoryFamily
from fragancia_api.shared.kernel import Err, Ok

CREATED_AT = datetime(2026, 10, 2, tzinfo=UTC)


def family_name(raw: str) -> FamilyName:
    result = FamilyName.create(raw)
    assert isinstance(result, Ok), result
    return result.value


def brand_name(raw: str) -> BrandName:
    result = BrandName.create(raw)
    assert isinstance(result, Ok), result
    return result.value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Maison   Margiela ", "Maison Margiela"),
        ("ab", "ab"),
        ("a" * 80, "a" * 80),
        ("Cítrica", "Cítrica"),
    ],
)
def test_clean_name_trims_and_collapses_whitespace(raw: str, expected: str) -> None:
    assert clean_name(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "x", "  y  ", "!!", "—·—", "a" * 81])
def test_clean_name_rejects_bad_lengths_and_names_without_letters_or_digits(raw: str) -> None:
    assert clean_name(raw) is None


def test_clean_name_rejects_a_name_whose_slug_outgrows_the_slug_column() -> None:
    # NFKD expands "Ⅷ" to "viii": 80 characters of name become a 320-character slug, which
    # the String(100) slug column refused with a 500 instead of a 422.
    assert clean_name("Ⅷ" * 80) is None
    assert clean_name("Ⅷ" * 25) == "Ⅷ" * 25  # exactly 100 characters of slug


def test_slugify_is_still_importable_from_the_brand_module() -> None:
    assert brand_module.slugify is slugify
    assert brand_module.slugify("Éclat d'Arpège") == "eclat-d-arpege"


@pytest.mark.parametrize(
    ("raw", "value", "slug"),
    [
        ("  Cítrica ", "Cítrica", "citrica"),
        ("Fougère", "Fougère", "fougere"),
        ("Acuática", "Acuática", "acuatica"),
        ("Oriental   Especiada", "Oriental Especiada", "oriental-especiada"),
    ],
)
def test_family_names_are_normalized_and_slugged(raw: str, value: str, slug: str) -> None:
    name = family_name(raw)
    assert (name.value, name.slug) == (value, slug)


def test_names_with_the_same_slug_are_the_same_family() -> None:
    assert family_name("Citrica").slug == family_name("CÍTRICA").slug


@pytest.mark.parametrize("raw", ["", "x", "!!", "a" * 81])
def test_invalid_family_names(raw: str) -> None:
    result = FamilyName.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, FamilyNameInvalid)
    assert result.error.code == "CATALOG_FAMILY_NAME_INVALID"
    assert result.error.details == {"min": 2, "max": 80}


def test_family_length_limits_are_inclusive() -> None:
    assert isinstance(FamilyName.create("ab"), Ok)
    assert isinstance(FamilyName.create("a" * 80), Ok)


def test_new_families_are_active_and_record_no_event() -> None:
    family = OlfactoryFamily.create(family_name("Amaderada"), created_at=CREATED_AT)

    assert family.is_active
    assert family.created_at == CREATED_AT
    assert family.slug == "amaderada"
    assert family.pull_events() == []


def test_renaming_a_family_changes_its_slug() -> None:
    family = OlfactoryFamily.create(family_name("Especiada"), created_at=CREATED_AT)

    family.rename(family_name("Especiada Cálida"))

    assert family.name.value == "Especiada Cálida"
    assert family.slug == "especiada-calida"
    assert family.pull_events() == []


def test_family_archive_and_restore_are_idempotent() -> None:
    family = OlfactoryFamily.create(family_name("Floral"), created_at=CREATED_AT)

    family.archive()
    family.archive()
    assert not family.is_active

    family.restore()
    family.restore()
    assert [family.is_active, family.pull_events()] == [True, []]


def test_renaming_a_brand_changes_its_slug_and_records_no_new_event() -> None:
    brand = Brand.create(brand_name("Dior"), created_at=CREATED_AT)
    brand.pull_events()

    brand.rename(brand_name("Christian Dior"))

    assert brand.name.value == "Christian Dior"
    assert brand.slug == "christian-dior"
    assert brand.pull_events() == []


def test_renaming_a_brand_to_the_same_slug_keeps_the_slug() -> None:
    brand = Brand.create(brand_name("Dior"), created_at=CREATED_AT)

    brand.rename(brand_name("DIOR"))

    assert (brand.name.value, brand.slug) == ("DIOR", "dior")


def test_brand_archive_and_restore_are_idempotent() -> None:
    brand = Brand.create(brand_name("Dior"), created_at=CREATED_AT)
    brand.pull_events()

    brand.archive()
    brand.archive()
    assert not brand.is_active

    brand.restore()
    brand.restore()
    assert [brand.is_active, brand.pull_events()] == [True, []]
