from datetime import UTC, datetime

import pytest

from fragancia_api.modules.catalog.domain.concentration import (
    Abbreviation,
    Concentration,
    ConcentrationName,
)
from fragancia_api.modules.catalog.domain.errors import (
    ConcentrationAbbreviationInvalid,
    ConcentrationNameInvalid,
)
from fragancia_api.modules.catalog.domain.naming import clean_abbreviation
from fragancia_api.shared.kernel import Err, Ok

CREATED_AT = datetime(2026, 10, 9, tzinfo=UTC)


def _name(raw: str) -> ConcentrationName:
    result = ConcentrationName.create(raw)
    assert isinstance(result, Ok), result
    return result.value


def _abbreviation(raw: str) -> Abbreviation:
    result = Abbreviation.create(raw)
    assert isinstance(result, Ok), result
    return result.value


def _concentration(name: str = "Eau de Toilette", abbreviation: str = "EDT") -> Concentration:
    return Concentration.create(_name(name), _abbreviation(abbreviation), created_at=CREATED_AT)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  EDT ", "EDT"),
        ("Extra   it", "Extra it"),
        ("ab", "ab"),
        ("a" * 12, "a" * 12),
        ("Extrait", "Extrait"),
    ],
)
def test_clean_abbreviation_trims_collapses_whitespace_and_keeps_the_case(
    raw: str, expected: str
) -> None:
    assert clean_abbreviation(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "x", "  y  ", "!!", "—·—", "a" * 13])
def test_clean_abbreviation_rejects_bad_lengths_and_texts_without_letters_or_digits(
    raw: str,
) -> None:
    assert clean_abbreviation(raw) is None


def test_clean_abbreviation_rejects_a_slug_longer_than_20_characters() -> None:
    # NFKD expands "Ⅷ" to "viii": 6 characters of text become 24 characters of slug.
    assert clean_abbreviation("Ⅷ" * 6) is None
    assert clean_abbreviation("Ⅷ" * 5) == "Ⅷ" * 5  # exactly 20 characters of slug


@pytest.mark.parametrize(
    ("raw", "value", "slug"),
    [
        ("  Eau   de Toilette ", "Eau de Toilette", "eau-de-toilette"),
        ("Extrait de Parfum", "Extrait de Parfum", "extrait-de-parfum"),
        ("Eau Fraîche", "Eau Fraîche", "eau-fraiche"),
    ],
)
def test_concentration_names_are_normalized_and_slugged(raw: str, value: str, slug: str) -> None:
    name = _name(raw)
    assert (name.value, name.slug) == (value, slug)


@pytest.mark.parametrize("raw", ["", "x", "!!", "a" * 81])
def test_invalid_concentration_names(raw: str) -> None:
    result = ConcentrationName.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, ConcentrationNameInvalid)
    assert result.error.code == "CATALOG_CONCENTRATION_NAME_INVALID"
    assert result.error.details == {"min": 2, "max": 80}


def test_concentration_name_length_limits_are_inclusive() -> None:
    assert isinstance(ConcentrationName.create("ab"), Ok)
    assert isinstance(ConcentrationName.create("a" * 80), Ok)


@pytest.mark.parametrize(
    ("raw", "value", "slug"),
    [("  EDT ", "EDT", "edt"), ("Extrait", "Extrait", "extrait"), ("E.D.P", "E.D.P", "e-d-p")],
)
def test_abbreviations_are_kept_as_typed_and_slugged(raw: str, value: str, slug: str) -> None:
    abbreviation = _abbreviation(raw)
    assert (abbreviation.value, abbreviation.slug) == (value, slug)


def test_abbreviations_with_the_same_slug_clash() -> None:
    assert _abbreviation("EDT").slug == _abbreviation("edt").slug


@pytest.mark.parametrize("raw", ["", "x", "!!", "a" * 13])
def test_invalid_abbreviations(raw: str) -> None:
    result = Abbreviation.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, ConcentrationAbbreviationInvalid)
    assert result.error.code == "CATALOG_CONCENTRATION_ABBREVIATION_INVALID"
    assert result.error.details == {"min": 2, "max": 12}


def test_abbreviation_length_limits_are_inclusive() -> None:
    assert isinstance(Abbreviation.create("ab"), Ok)
    assert isinstance(Abbreviation.create("a" * 12), Ok)


def test_new_concentrations_are_active_and_record_no_event() -> None:
    concentration = _concentration()

    assert concentration.is_active
    assert concentration.created_at == CREATED_AT
    assert (concentration.slug, concentration.abbreviation_slug) == ("eau-de-toilette", "edt")
    assert concentration.pull_events() == []


def test_updating_a_concentration_changes_both_texts_and_both_slugs() -> None:
    concentration = _concentration()

    concentration.update(_name("Body Mist"), _abbreviation("Mist"))

    assert (concentration.name.value, concentration.abbreviation.value) == ("Body Mist", "Mist")
    assert (concentration.slug, concentration.abbreviation_slug) == ("body-mist", "mist")
    assert concentration.pull_events() == []


def test_updating_keeps_the_identity_and_the_active_flag() -> None:
    concentration = _concentration()
    original_id = concentration.id
    concentration.archive()

    concentration.update(_name("Eau de Toilette Intense"), _abbreviation("EDTI"))

    assert (concentration.id, concentration.is_active) == (original_id, False)


def test_concentration_archive_and_restore_are_idempotent() -> None:
    concentration = _concentration()

    concentration.archive()
    concentration.archive()
    assert not concentration.is_active

    concentration.restore()
    concentration.restore()
    assert [concentration.is_active, concentration.pull_events()] == [True, []]
