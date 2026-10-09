from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    Perfume,
    PerfumeName,
    Presentation,
    Price,
    Sale,
    perfume_slug,
)
from fragancia_api.shared.kernel import Money, Ok, new_id
from tests.unit.catalog.perfume_support import (
    LATER,
    NOW,
    add_presentation,
    error_code,
    error_details,
    make_brand,
    make_concentration,
    make_family,
    make_perfume,
    unwrap_ok,
)

UNKNOWN = UUID(int=404)


def _perfume() -> Perfume:
    return make_perfume(make_brand(), make_family(), make_concentration())


def _price(cents: int = 250_000) -> Price:
    return unwrap_ok(Price.create(cents))


def _in_stock() -> Availability:
    return unwrap_ok(Availability.create("in_stock", None, None))


# --- values -----------------------------------------------------------------------------------


def test_perfume_name_follows_the_clean_name_rules() -> None:
    name = unwrap_ok(PerfumeName.create("  Eros   Flame "))
    assert (name.value, name.slug) == ("Eros Flame", "eros-flame")


@pytest.mark.parametrize("raw", ["", "x", "!!", "a" * 81])
def test_perfume_name_rejects_invalid_names(raw: str) -> None:
    result = PerfumeName.create(raw)
    assert error_code(result) == "CATALOG_PERFUME_NAME_INVALID"
    assert error_details(result) == {"min": 2, "max": 80}


def test_perfume_name_limits_are_inclusive() -> None:
    assert isinstance(PerfumeName.create("ab"), Ok)
    assert isinstance(PerfumeName.create("a" * 80), Ok)


def test_description_is_trimmed_and_may_be_empty() -> None:
    assert unwrap_ok(Description.create("  fresh  ")).value == "fresh"
    assert unwrap_ok(Description.create("")).value == ""


def test_description_limit_is_2000_inclusive() -> None:
    assert isinstance(Description.create("a" * 2000), Ok)
    result = Description.create("a" * 2001)
    assert error_code(result) == "CATALOG_PERFUME_DESCRIPTION_TOO_LONG"
    assert error_details(result) == {"max": 2000}


def test_description_length_is_counted_after_trimming() -> None:
    assert isinstance(Description.create("  " + "a" * 2000 + "  "), Ok)


def test_notes_are_trimmed_collapsed_and_keep_order_and_duplicates() -> None:
    notes = unwrap_ok(Notes.create([" Pink  Pepper ", "Lemon", "Lemon"], ["Rose"], []))
    assert notes.top == ("Pink Pepper", "Lemon", "Lemon")
    assert notes.heart == ("Rose",)
    assert notes.base == ()


def test_notes_limits_are_inclusive() -> None:
    assert isinstance(Notes.create(["a"] * 10, ["a" * 40], ["a"] * 10), Ok)


@pytest.mark.parametrize(
    ("top", "heart", "base"),
    [
        (["a"] * 11, [], []),
        ([], ["a"] * 11, []),
        ([], [], ["a"] * 11),
        (["a" * 41], [], []),
        ([], ["a" * 41], []),
        ([], [], ["a" * 41]),
        ([""], [], []),
        ([], ["   "], []),
        ([], [], ["ok", ""]),
    ],
)
def test_notes_reject_too_many_too_long_or_empty_notes(
    top: list[str], heart: list[str], base: list[str]
) -> None:
    result = Notes.create(top, heart, base)
    assert error_code(result) == "CATALOG_PERFUME_NOTES_INVALID"
    assert error_details(result) == {"max_per_level": 10, "max_length": 40}


@pytest.mark.parametrize("value", [1, 100, 1000])
def test_ml_accepts_1_to_1000(value: int) -> None:
    assert unwrap_ok(Ml.create(value)).value == value


@pytest.mark.parametrize("value", [0, -1, 1001])
def test_ml_rejects_out_of_range(value: int) -> None:
    result = Ml.create(value)
    assert error_code(result) == "CATALOG_PRESENTATION_ML_INVALID"
    assert error_details(result) == {"min": 1, "max": 1000}


@pytest.mark.parametrize("cents", [1, 250_000, 10_000_000])
def test_price_accepts_1_to_10_000_000_cents(cents: int) -> None:
    assert unwrap_ok(Price.create(cents)).amount == Money(cents)


@pytest.mark.parametrize("cents", [0, -5, 10_000_001])
def test_price_rejects_out_of_range(cents: int) -> None:
    result = Price.create(cents)
    assert error_code(result) == "CATALOG_PRESENTATION_PRICE_INVALID"
    assert error_details(result) == {"min_cents": 1, "max_cents": 10_000_000}


# --- sale -------------------------------------------------------------------------------------


def test_no_sale_price_and_no_dates_is_no_sale() -> None:
    assert unwrap_ok(Sale.create(None, None, None, regular=_price())) is None


def test_sale_with_price_only_has_no_window() -> None:
    sale = unwrap_ok(Sale.create(200_000, None, None, regular=_price()))
    assert sale == Sale(Money(200_000), None, None)


def test_sale_with_open_ended_windows() -> None:
    from_only = unwrap_ok(Sale.create(200_000, NOW, None, regular=_price()))
    until_only = unwrap_ok(Sale.create(200_000, None, LATER, regular=_price()))
    assert from_only == Sale(Money(200_000), NOW, None)
    assert until_only == Sale(Money(200_000), None, LATER)


def test_sale_price_boundaries() -> None:
    assert isinstance(Sale.create(1, None, None, regular=_price()), Ok)
    assert isinstance(Sale.create(249_999, None, None, regular=_price()), Ok)
    for cents in (0, -1, 250_000, 300_000):
        result = Sale.create(cents, None, None, regular=_price())
        assert error_code(result) == "CATALOG_PRESENTATION_SALE_INVALID"


def test_sale_window_must_end_after_it_starts() -> None:
    assert isinstance(Sale.create(100, NOW, NOW + timedelta(seconds=1), regular=_price()), Ok)
    for ends in (NOW, NOW - timedelta(days=1)):
        result = Sale.create(100, NOW, ends, regular=_price())
        assert error_code(result) == "CATALOG_PRESENTATION_SALE_INVALID"


def test_sale_allows_a_past_window() -> None:
    past = datetime(2020, 1, 1, tzinfo=UTC)
    assert isinstance(Sale.create(100, past, past + timedelta(days=1), regular=_price()), Ok)


def test_sale_dates_must_be_timezone_aware() -> None:
    naive = datetime(2026, 1, 1)  # noqa: DTZ001
    assert error_code(Sale.create(100, naive, None, regular=_price())) == (
        "CATALOG_PRESENTATION_SALE_INVALID"
    )
    assert error_code(Sale.create(100, None, naive, regular=_price())) == (
        "CATALOG_PRESENTATION_SALE_INVALID"
    )


def test_sale_dates_without_a_price_are_invalid() -> None:
    assert error_code(Sale.create(None, NOW, None, regular=_price())) == (
        "CATALOG_PRESENTATION_SALE_INVALID"
    )
    assert error_code(Sale.create(None, None, LATER, regular=_price())) == (
        "CATALOG_PRESENTATION_SALE_INVALID"
    )


# --- availability -----------------------------------------------------------------------------


def test_in_stock_has_no_lead_time() -> None:
    assert unwrap_ok(Availability.create("in_stock", None, None)) == Availability(
        "in_stock", None, None
    )


@pytest.mark.parametrize(("low", "high"), [(1, None), (None, 5), (1, 5)])
def test_in_stock_with_any_lead_time_is_invalid(low: int | None, high: int | None) -> None:
    result = Availability.create("in_stock", low, high)
    assert error_code(result) == "CATALOG_PRESENTATION_AVAILABILITY_INVALID"
    assert error_details(result) == {"min_days": 1, "max_days": 90}


@pytest.mark.parametrize(("low", "high"), [(1, 1), (7, 10), (1, 90), (90, 90)])
def test_made_to_order_accepts_valid_lead_times(low: int, high: int) -> None:
    assert unwrap_ok(Availability.create("made_to_order", low, high)) == Availability(
        "made_to_order", low, high
    )


@pytest.mark.parametrize(
    ("low", "high"),
    [(None, None), (7, None), (None, 7), (0, 5), (1, 91), (10, 7), (-1, 5), (91, 91)],
)
def test_made_to_order_rejects_missing_out_of_range_or_inverted_lead_times(
    low: int | None, high: int | None
) -> None:
    assert error_code(Availability.create("made_to_order", low, high)) == (
        "CATALOG_PRESENTATION_AVAILABILITY_INVALID"
    )


# --- slug -------------------------------------------------------------------------------------


def test_perfume_slug_joins_brand_name_and_abbreviation() -> None:
    name = unwrap_ok(PerfumeName.create("Eros"))
    assert perfume_slug("Versace", name, "EDT") == "versace-eros-edt"


def test_perfume_slug_strips_accents_and_symbols() -> None:
    name = unwrap_ok(PerfumeName.create("Éclat d'Arpège"))
    assert perfume_slug("Dolce & Gabbana", name, "Extrait") == (
        "dolce-gabbana-eclat-d-arpege-extrait"
    )


# --- aggregate: creation and update -----------------------------------------------------------


def test_a_new_perfume_is_hidden_unarchived_and_without_presentations() -> None:
    perfume = _perfume()

    assert (perfume.is_published, perfume.is_archived) == (False, False)
    assert perfume.first_published_at is None
    assert perfume.presentations == []
    assert (perfume.created_at, perfume.updated_at) == (NOW, NOW)
    assert perfume.slug == "versace-eros-edt"
    assert perfume.name_slug == "eros"


def test_update_replaces_every_field_and_touches_updated_at() -> None:
    perfume = _perfume()
    brand_id, family_id, concentration_id = new_id(), new_id(), new_id()
    name = unwrap_ok(PerfumeName.create("Eros Flame"))
    notes = unwrap_ok(Notes.create(["Lemon"], ["Rose"], ["Musk"]))

    result = perfume.update(
        brand_id=brand_id,
        concentration_id=concentration_id,
        family_id=family_id,
        name=name,
        slug="x-eros-flame-y",
        gender=Gender.UNISEX,
        description=unwrap_ok(Description.create("new")),
        notes=notes,
        now=LATER,
    )

    assert isinstance(result, Ok)
    assert (perfume.brand_id, perfume.family_id, perfume.concentration_id) == (
        brand_id,
        family_id,
        concentration_id,
    )
    assert (perfume.name.value, perfume.slug, perfume.gender) == (
        "Eros Flame",
        "x-eros-flame-y",
        Gender.UNISEX,
    )
    assert perfume.description.value == "new"
    assert perfume.notes == notes
    assert (perfume.created_at, perfume.updated_at) == (NOW, LATER)


def test_an_archived_perfume_cannot_be_updated() -> None:
    perfume = _perfume()
    perfume.archive()

    result = perfume.update(
        brand_id=perfume.brand_id,
        concentration_id=perfume.concentration_id,
        family_id=perfume.family_id,
        name=perfume.name,
        slug="other",
        gender=perfume.gender,
        description=perfume.description,
        notes=perfume.notes,
        now=LATER,
    )

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert (perfume.slug, perfume.updated_at) == ("versace-eros-edt", NOW)


# --- aggregate: publish, hide, archive, restore -----------------------------------------------


def test_publish_needs_an_active_presentation() -> None:
    perfume = _perfume()

    assert error_code(perfume.publish(NOW)) == "CATALOG_PERFUME_NOTHING_TO_SELL"
    assert (perfume.is_published, perfume.first_published_at) == (False, None)


def test_publish_ignores_archived_presentations() -> None:
    perfume = _perfume()
    presentation_id = add_presentation(perfume)
    assert isinstance(perfume.archive_presentation(presentation_id), Ok)

    assert error_code(perfume.publish(NOW)) == "CATALOG_PERFUME_NOTHING_TO_SELL"


def test_publish_sets_first_published_at_once_and_is_idempotent() -> None:
    perfume = _perfume()
    add_presentation(perfume)

    assert isinstance(perfume.publish(NOW), Ok)
    assert isinstance(perfume.publish(LATER), Ok)

    assert perfume.is_published
    assert perfume.first_published_at == NOW


def test_republishing_after_hide_keeps_the_first_publication_date() -> None:
    perfume = _perfume()
    add_presentation(perfume)
    perfume.publish(NOW)
    perfume.hide()

    hidden = (perfume.is_published, perfume.first_published_at)
    assert isinstance(perfume.publish(LATER), Ok)

    assert hidden == (False, NOW)
    assert (perfume.is_published, perfume.first_published_at) == (True, NOW)


def test_an_archived_perfume_cannot_be_published() -> None:
    perfume = _perfume()
    add_presentation(perfume)
    perfume.archive()

    assert error_code(perfume.publish(NOW)) == "CATALOG_PERFUME_ARCHIVED"
    assert not perfume.is_published


def test_hide_is_idempotent() -> None:
    perfume = _perfume()
    perfume.hide()
    perfume.hide()
    assert not perfume.is_published


def test_archive_also_hides_and_is_idempotent() -> None:
    perfume = _perfume()
    add_presentation(perfume)
    perfume.publish(NOW)

    perfume.archive()
    perfume.archive()

    assert (perfume.is_archived, perfume.is_published) == (True, False)
    assert perfume.first_published_at == NOW


def test_restore_leaves_the_perfume_hidden_and_is_idempotent() -> None:
    perfume = _perfume()
    add_presentation(perfume)
    perfume.publish(NOW)
    perfume.archive()

    perfume.restore()
    perfume.restore()

    assert (perfume.is_archived, perfume.is_published) == (False, False)


def test_hide_works_on_an_archived_perfume() -> None:
    perfume = _perfume()
    perfume.archive()
    perfume.hide()
    assert (perfume.is_archived, perfume.is_published) == (True, False)


# --- aggregate: presentations -----------------------------------------------------------------


def test_adding_a_presentation_makes_it_active_and_touches_updated_at() -> None:
    perfume = _perfume()

    result = perfume.add_presentation(Ml(100), _price(), None, _in_stock(), created_at=LATER)

    presentation_id = unwrap_ok(result)
    [presentation] = perfume.presentations
    assert isinstance(presentation, Presentation)
    assert presentation.id == presentation_id
    assert presentation.is_active
    assert presentation.created_at == LATER
    assert (presentation.ml, presentation.price) == (Ml(100), _price())
    assert perfume.updated_at == LATER


def test_adding_the_same_ml_is_a_conflict_even_when_archived() -> None:
    perfume = _perfume()
    first = add_presentation(perfume, ml=100)

    duplicate = perfume.add_presentation(Ml(100), _price(), None, _in_stock(), created_at=LATER)
    perfume.archive_presentation(first)
    duplicate_of_archived = perfume.add_presentation(
        Ml(100), _price(), None, _in_stock(), created_at=LATER
    )

    assert error_code(duplicate) == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert error_code(duplicate_of_archived) == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert len(perfume.presentations) == 1
    assert perfume.updated_at == NOW


def test_an_archived_perfume_refuses_presentation_changes() -> None:
    perfume = _perfume()
    presentation_id = add_presentation(perfume)
    perfume.archive()

    codes = [
        error_code(
            perfume.add_presentation(Ml(200), _price(), None, _in_stock(), created_at=LATER)
        ),
        error_code(
            perfume.update_presentation(
                presentation_id, Ml(200), _price(), None, _in_stock(), now=LATER
            )
        ),
        error_code(perfume.archive_presentation(presentation_id)),
        error_code(perfume.restore_presentation(presentation_id)),
    ]

    assert codes == ["CATALOG_PERFUME_ARCHIVED"] * 4
    assert [p.ml.value for p in perfume.presentations] == [100]


def test_update_presentation_replaces_its_fields_and_keeps_its_state() -> None:
    perfume = _perfume()
    presentation_id = add_presentation(perfume)
    sale = Sale(Money(100_000), None, LATER)
    availability = unwrap_ok(Availability.create("made_to_order", 7, 10))

    result = perfume.update_presentation(
        presentation_id, Ml(200), _price(390_000), sale, availability, now=LATER
    )

    assert isinstance(result, Ok)
    [presentation] = perfume.presentations
    assert presentation.ml == Ml(200)
    assert presentation.price == _price(390_000)
    assert presentation.sale == sale
    assert presentation.availability == availability
    assert presentation.is_active
    assert presentation.created_at == NOW
    assert perfume.updated_at == LATER


def test_update_presentation_may_keep_its_own_ml() -> None:
    perfume = _perfume()
    presentation_id = add_presentation(perfume, ml=100)

    result = perfume.update_presentation(
        presentation_id, Ml(100), _price(300_000), None, _in_stock(), now=LATER
    )

    assert isinstance(result, Ok)
    assert perfume.presentations[0].price == _price(300_000)


def test_update_presentation_refuses_the_ml_of_another_one() -> None:
    perfume = _perfume()
    add_presentation(perfume, ml=100)
    second = add_presentation(perfume, ml=200)

    result = perfume.update_presentation(second, Ml(100), _price(), None, _in_stock(), now=LATER)

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert [p.ml.value for p in perfume.presentations] == [100, 200]
    assert perfume.updated_at == NOW


def test_update_presentation_refuses_the_ml_of_an_archived_one() -> None:
    perfume = _perfume()
    first = add_presentation(perfume, ml=100)
    second = add_presentation(perfume, ml=200)
    perfume.archive_presentation(first)

    result = perfume.update_presentation(second, Ml(100), _price(), None, _in_stock(), now=LATER)

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"


def test_unknown_presentations_are_not_found() -> None:
    perfume = _perfume()
    add_presentation(perfume)

    codes = [
        error_code(
            perfume.update_presentation(UNKNOWN, Ml(200), _price(), None, _in_stock(), now=LATER)
        ),
        error_code(perfume.archive_presentation(UNKNOWN)),
        error_code(perfume.restore_presentation(UNKNOWN)),
    ]

    assert codes == ["CATALOG_PRESENTATION_NOT_FOUND"] * 3


def test_archive_and_restore_a_presentation_are_idempotent() -> None:
    perfume = _perfume()
    keep = add_presentation(perfume, ml=100)
    other = add_presentation(perfume, ml=200)
    other_presentation = perfume.presentations[1]

    assert isinstance(perfume.archive_presentation(other), Ok)
    assert isinstance(perfume.archive_presentation(other), Ok)
    archived_state = other_presentation.is_active
    assert isinstance(perfume.restore_presentation(other), Ok)
    assert isinstance(perfume.restore_presentation(other), Ok)

    assert archived_state is False
    assert other_presentation.is_active
    assert perfume.presentations[0].id == keep
    assert perfume.presentations[0].is_active


def test_the_last_active_presentation_of_a_published_perfume_cannot_be_archived() -> None:
    perfume = _perfume()
    first = add_presentation(perfume, ml=100)
    second = add_presentation(perfume, ml=200)
    perfume.publish(NOW)

    assert isinstance(perfume.archive_presentation(second), Ok)
    result = perfume.archive_presentation(first)

    assert error_code(result) == "CATALOG_PERFUME_LAST_PRESENTATION"
    assert perfume.presentations[0].is_active


def test_the_last_active_presentation_of_a_hidden_perfume_can_be_archived() -> None:
    perfume = _perfume()
    only = add_presentation(perfume)

    assert isinstance(perfume.archive_presentation(only), Ok)
    assert not perfume.presentations[0].is_active


def test_archiving_an_already_archived_presentation_of_a_published_perfume_is_ok() -> None:
    perfume = _perfume()
    first = add_presentation(perfume, ml=100)
    second = add_presentation(perfume, ml=200)
    perfume.archive_presentation(second)
    perfume.publish(NOW)

    assert isinstance(perfume.archive_presentation(second), Ok)
    assert error_code(perfume.archive_presentation(first)) == "CATALOG_PERFUME_LAST_PRESENTATION"


def test_a_published_perfume_can_archive_one_when_another_active_remains() -> None:
    perfume = _perfume()
    first = add_presentation(perfume, ml=100)
    add_presentation(perfume, ml=200)
    perfume.publish(NOW)

    assert isinstance(perfume.archive_presentation(first), Ok)
    assert [p.is_active for p in perfume.presentations] == [False, True]
