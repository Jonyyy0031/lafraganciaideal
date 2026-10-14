"""Plan 007: the cell rules of the Excel import (pure functions of `domain/import_cells.py`)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from fragancia_api.modules.catalog.domain import import_cells as cells
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.catalog.perfume_support import error_code, error_details, unwrap_ok

INVALID = "CATALOG_IMPORT_VALUE_INVALID"


# --- the header contract ----------------------------------------------------------------------


def test_the_template_has_the_22_spanish_headers_in_order() -> None:
    assert cells.COLUMNS == (
        "Marca",
        "Nombre",
        "Concentración",
        "Género",
        "Familia olfativa",
        "ML",
        "Precio",
        "Precio oferta",
        "Oferta desde",
        "Oferta hasta",
        "Disponibilidad",
        "Entrega mín. (días)",
        "Entrega máx. (días)",
        "Existencias",
        "Publicado",
        "Notas de salida",
        "Notas de corazón",
        "Notas de fondo",
        "Descripción",
        "Imagen 1",
        "Imagen 2",
        "Imagen 3",
    )
    assert len(cells.COLUMNS) == 22


def test_the_required_columns_are_the_eight_of_the_plan_and_all_are_template_columns() -> None:
    assert cells.REQUIRED_COLUMNS == (
        "Marca",
        "Nombre",
        "Concentración",
        "Género",
        "Familia olfativa",
        "ML",
        "Precio",
        "Disponibilidad",
    )
    assert set(cells.REQUIRED_COLUMNS) <= set(cells.COLUMNS)


@pytest.mark.parametrize(
    ("header", "key"),
    [
        ("Familia olfativa", "familia-olfativa"),
        ("  FAMILIA   Olfativa ", "familia-olfativa"),
        ("Concentración", "concentracion"),
        ("CONCENTRACION", "concentracion"),
        ("Entrega mín. (días)", "entrega-min-dias"),
    ],
)
def test_headers_are_matched_by_key_ignoring_case_accents_and_punctuation(
    header: str, key: str
) -> None:
    assert cells.header_key(header) == key


def test_every_template_header_has_a_distinct_key() -> None:
    keys = [cells.header_key(column) for column in cells.COLUMNS]

    assert len(set(keys)) == len(keys)


# --- text -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        (None, ""),
        ("  Eros  ", "Eros"),
        ("", ""),
        (212, "212"),
        (212.0, "212"),
        ("Dior Sauvage", "Dior Sauvage"),
    ],
)
def test_text_reads_none_strings_and_whole_numbers(cell: cells.Cell, expected: str) -> None:
    assert unwrap_ok(cells.text(cell)) == expected


@pytest.mark.parametrize("cell", [1.5, True, False, date(2026, 1, 1), float("nan"), float("inf")])
def test_text_rejects_fractions_booleans_dates_and_non_finite_numbers(cell: cells.Cell) -> None:
    assert error_code(cells.text(cell)) == INVALID


def test_text_accepts_4000_characters_and_rejects_4001() -> None:
    assert unwrap_ok(cells.text("x" * 4000)) == "x" * 4000

    result = cells.text("x" * 4001)

    assert error_code(result) == INVALID
    assert isinstance(result, Err)
    assert result.error.details == {"max_length": 4000}


def test_text_measures_the_limit_after_stripping() -> None:
    assert unwrap_ok(cells.text("  " + "x" * 4000 + "  ")) == "x" * 4000


# --- stock_count (plan 005) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [(5, 5), (5.0, 5), ("5", 5), (0, 0), (cells.STOCK_COUNT_MAX, cells.STOCK_COUNT_MAX)],
)
def test_stock_count_accepts_whole_numbers_from_zero_to_the_maximum(
    cell: cells.Cell, expected: int
) -> None:
    assert unwrap_ok(cells.stock_count(cell)) == expected


@pytest.mark.parametrize("cell", [-1, cells.STOCK_COUNT_MAX + 1, "100001"])
def test_stock_count_out_of_range_is_a_value_error_carrying_the_bounds(cell: cells.Cell) -> None:
    result = cells.stock_count(cell)

    assert error_code(result) == INVALID
    assert error_details(result) == {"min": 0, "max": cells.STOCK_COUNT_MAX}


@pytest.mark.parametrize("cell", [2.5, "cinco", True, False, None, "", float("nan")])
def test_stock_count_rejects_non_integral_text_and_booleans(cell: cells.Cell) -> None:
    assert error_code(cells.stock_count(cell)) == INVALID


def test_existencias_is_a_template_column_right_after_the_max_lead_time_and_not_required() -> None:
    assert cells.COLUMNS[cells.COLUMNS.index("Entrega máx. (días)") + 1] == "Existencias"
    assert "Existencias" not in cells.REQUIRED_COLUMNS


# --- whole_number -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [(100, 100), (100.0, 100), ("100", 100), (" 100 ", 100), (0, 0), ("007", 7)],
)
def test_whole_number_accepts_ints_integral_floats_and_digit_strings(
    cell: cells.Cell, expected: int
) -> None:
    assert unwrap_ok(cells.whole_number(cell)) == expected


@pytest.mark.parametrize(
    "cell",
    [
        100.5,
        "abc",
        "10.5",
        "-5",
        "",
        "1 0",
        None,
        True,
        float("nan"),
        float("inf"),
        "١٢٣",  # Arabic-Indic digits are digits for str.isdigit but not ASCII
        "²",
    ],
)
def test_whole_number_rejects_everything_else(cell: cells.Cell) -> None:
    assert error_code(cells.whole_number(cell)) == INVALID


# --- pesos_to_cents (decision 67) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "cents"),
    [
        (2500, 250_000),
        (2500.0, 250_000),
        (10.5, 1050),
        (10.55, 1055),
        (0.1, 10),
        (0.29, 29),  # float 0.29 * 100 is 28.999...: the Decimal(str(x)) path must give 29
        (19.99, 1999),
        ("10", 1000),
        ("10.5", 1050),
        ("10.55", 1055),
        (" 10.55 ", 1055),
        (0, 0),
    ],
)
def test_pesos_to_cents_converts_up_to_two_decimals_exactly(cell: cells.Cell, cents: int) -> None:
    assert unwrap_ok(cells.pesos_to_cents(cell)) == cents


@pytest.mark.parametrize(
    "cell",
    [
        10.555,  # 3 decimals
        "10.555",
        "10.",
        ".5",
        "-1",
        -1,
        -0.01,
        "1,000.50",
        "$10",
        "abc",
        "",
        None,
        True,
        float("nan"),
        float("inf"),
        float("-inf"),
        date(2026, 1, 1),
    ],
)
def test_pesos_to_cents_rejects_three_decimals_negatives_non_finite_and_text(
    cell: cells.Cell,
) -> None:
    assert error_code(cells.pesos_to_cents(cell)) == INVALID


# --- sale_day (decision 66) -------------------------------------------------------------------


def test_sale_day_accepts_a_date() -> None:
    assert unwrap_ok(cells.sale_day(date(2026, 11, 1))) == date(2026, 11, 1)


def test_sale_day_accepts_a_datetime_at_exactly_midnight() -> None:
    assert unwrap_ok(cells.sale_day(datetime(2026, 11, 1, 0, 0, 0))) == date(2026, 11, 1)


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 1, 0, 0, 1),
        datetime(2026, 11, 1, 12, 0),
        datetime(2026, 11, 1, 0, 0, 0, 1),
        datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
    ],
)
def test_sale_day_rejects_a_datetime_that_is_not_naive_midnight(moment: datetime) -> None:
    assert error_code(cells.sale_day(moment)) == INVALID


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01/11/2026", date(2026, 11, 1)),
        ("1/11/2026", date(2026, 11, 1)),
        ("2026-11-01", date(2026, 11, 1)),
        (" 30/11/2026 ", date(2026, 11, 30)),
        ("29/02/2028", date(2028, 2, 29)),
    ],
)
def test_sale_day_accepts_dd_mm_yyyy_and_iso_strings(raw: str, expected: date) -> None:
    assert unwrap_ok(cells.sale_day(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026/11/01",
        "11/30/2026",
        "31/04/2026",
        "29/02/2027",
        "01-11-2026",
        "mañana",
        "",
        "01/11/26",
    ],
)
def test_sale_day_rejects_other_strings_and_impossible_days(raw: str) -> None:
    assert error_code(cells.sale_day(raw)) == INVALID


@pytest.mark.parametrize("cell", [None, 20261101, 46327.0, True])
def test_sale_day_rejects_numbers_booleans_and_none(cell: Any) -> None:
    assert error_code(cells.sale_day(cell)) == INVALID


# --- sale_window: whole days in Cancún (UTC-5, no DST) ----------------------------------------


def test_the_sale_window_starts_at_midnight_of_desde_and_ends_at_midnight_after_hasta() -> None:
    starts_at, ends_at = cells.sale_window(date(2026, 11, 1), date(2026, 11, 30))

    assert starts_at is not None
    assert ends_at is not None
    assert starts_at.isoformat() == "2026-11-01T00:00:00-05:00"
    assert ends_at.isoformat() == "2026-12-01T00:00:00-05:00"
    assert starts_at.utcoffset() == timedelta(hours=-5)
    assert (starts_at.hour, starts_at.minute, ends_at.hour, ends_at.minute) == (0, 0, 0, 0)


def test_the_window_end_rolls_over_a_month_and_a_year_and_a_leap_day() -> None:
    _, new_year = cells.sale_window(None, date(2026, 12, 31))
    _, leap = cells.sale_window(None, date(2028, 2, 28))

    assert new_year is not None
    assert leap is not None
    assert new_year.isoformat() == "2027-01-01T00:00:00-05:00"
    assert leap.isoformat() == "2028-02-29T00:00:00-05:00"


def test_a_one_day_sale_lasts_exactly_24_hours() -> None:
    starts_at, ends_at = cells.sale_window(date(2026, 11, 1), date(2026, 11, 1))

    assert starts_at is not None
    assert ends_at is not None
    assert ends_at - starts_at == timedelta(hours=24)


def test_either_bound_may_be_missing() -> None:
    assert cells.sale_window(None, None) == (None, None)
    starts_at, ends_at = cells.sale_window(date(2026, 11, 1), None)
    assert starts_at is not None
    assert starts_at.isoformat() == "2026-11-01T00:00:00-05:00"
    assert ends_at is None
    starts_at, ends_at = cells.sale_window(None, date(2026, 11, 30))
    assert starts_at is None
    assert ends_at is not None
    assert ends_at.isoformat() == "2026-12-01T00:00:00-05:00"


def test_the_window_is_in_the_cancun_zone_not_utc() -> None:
    starts_at, _ = cells.sale_window(date(2026, 7, 1), None)

    assert starts_at is not None
    assert starts_at.tzinfo is cells.SALE_TIMEZONE
    assert starts_at.astimezone(UTC).time() == time(5, 0)  # no daylight saving in Cancún


# --- gender, availability, published ----------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("Mujer", "women"),
        ("mujer", "women"),
        ("MUJER", "women"),
        ("Hombre", "men"),
        ("  hombre ", "men"),
        ("Unisex", "unisex"),
        ("UNISEX", "unisex"),
    ],
)
def test_gender_matches_the_spanish_words_ignoring_case(word: str, expected: str) -> None:
    assert unwrap_ok(cells.gender(word)) == expected


@pytest.mark.parametrize("word", ["Women", "men", "Mujeres", "", "otro"])
def test_gender_rejects_other_words(word: str) -> None:
    assert error_code(cells.gender(word)) == INVALID


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("En existencia", "in_stock"),
        ("en existencia", "in_stock"),
        ("EN EXISTENCIA", "in_stock"),
        ("Sobre pedido", "made_to_order"),
        ("sobre  pedido", "made_to_order"),
        ("SOBRE PEDIDO", "made_to_order"),
    ],
)
def test_availability_matches_the_spanish_words_ignoring_case(word: str, expected: str) -> None:
    assert unwrap_ok(cells.availability(word)) == expected


@pytest.mark.parametrize("word", ["in_stock", "Existencia", "Agotado", ""])
def test_availability_rejects_other_words(word: str) -> None:
    assert error_code(cells.availability(word)) == INVALID


@pytest.mark.parametrize(
    ("word", "expected"),
    [("Sí", True), ("si", True), ("SÍ", True), ("No", False), ("no", False), ("NO", False)],
)
def test_published_matches_si_and_no_ignoring_case_and_accents(word: str, expected: bool) -> None:
    assert unwrap_ok(cells.published(word)) is expected


@pytest.mark.parametrize("word", ["", "   "])
def test_an_empty_published_cell_is_none_meaning_leave_it_as_it_is(word: str) -> None:
    assert unwrap_ok(cells.published(word)) is None


@pytest.mark.parametrize("word", ["Quizá", "true", "1", "nope"])
def test_published_rejects_other_words(word: str) -> None:
    assert error_code(cells.published(word)) == INVALID


# --- notes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        ("   ", []),
        ("Bergamota", ["Bergamota"]),
        ("Bergamota, Limón ,Pimienta", ["Bergamota", "Limón", "Pimienta"]),
        (",,Bergamota,, ,Limón,", ["Bergamota", "Limón"]),
        (",", []),
        ("Té verde, Rosa de Damasco", ["Té verde", "Rosa de Damasco"]),
    ],
)
def test_notes_splits_on_commas_strips_and_drops_empty_items(
    value: str, expected: list[str]
) -> None:
    assert cells.notes(value) == expected


# --- image_url --------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_image_cell_is_none(value: str) -> None:
    assert unwrap_ok(cells.image_url(value)) is None


@pytest.mark.parametrize(
    "url", ["http://example.test/a.jpg", "https://example.test/a.jpg", "  https://x.test/a.png  "]
)
def test_image_url_accepts_http_and_https(url: str) -> None:
    assert unwrap_ok(cells.image_url(url)) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/a.jpg",
        "example.test/a.jpg",
        "//example.test/a.jpg",
        "file:///etc/passwd",
    ],
)
def test_image_url_rejects_other_schemes_with_its_own_code(url: str) -> None:
    result = cells.image_url(url)

    assert error_code(result) == "CATALOG_IMPORT_IMAGE_URL_INVALID"


def test_image_url_accepts_2000_characters_and_rejects_2001() -> None:
    prefix = "https://example.test/"
    ok = prefix + "a" * (2000 - len(prefix))
    too_long = prefix + "a" * (2001 - len(prefix))

    assert isinstance(cells.image_url(ok), Ok)
    result = cells.image_url(too_long)
    assert error_code(result) == "CATALOG_IMPORT_IMAGE_URL_INVALID"
    assert isinstance(result, Err)
    assert result.error.details == {"max_length": 2000}
