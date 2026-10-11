"""Plan 007: `ImportValidator` over in-memory references, a fake downloader and image processor."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.application.import_validation import ImportValidator
from fragancia_api.modules.catalog.application.ports import SheetRows
from fragancia_api.modules.catalog.domain import import_cells as cells
from fragancia_api.modules.catalog.domain.catalog_import import ImportIssue
from fragancia_api.modules.catalog.domain.errors import (
    ImportImageHostBlocked,
    ImportImageUnreachable,
    PhotoFormatInvalid,
    PhotoTooLarge,
)
from fragancia_api.modules.catalog.domain.perfume import Gender, photo_keys
from fragancia_api.shared.kernel import DomainError, Ok, Result
from tests.unit.catalog.import_support import (
    BRAND,
    BRAND_ARCHIVED,
    BRAND_B,
    CONCENTRATION,
    CONCENTRATION_ARCHIVED,
    CONCENTRATION_B,
    FAMILY,
    FAMILY_ARCHIVED,
    FAMILY_B,
    IMAGE,
    URL_1,
    URL_2,
    URL_3,
    ValidatorRig,
    existing,
    identity,
    make_rig,
    row,
    sheet,
)

PNG = b"png-bytes"
JPG = b"jpg-bytes"
BOTH = {URL_1: PNG, URL_2: JPG, URL_3: b"third"}
OLD_PHOTO = UUID(int=701)
OLD_PHOTO_2 = UUID(int=702)
EXISTING_ID = UUID(int=900)
ARCHIVED_PRESENTATION = UUID(int=801)
ACTIVE_PRESENTATION = UUID(int=802)


def _report(issues: list[ImportIssue]) -> list[tuple[int | None, str | None, str]]:
    return [(issue.row, issue.column, issue.code) for issue in issues]


async def _validate(rig: ValidatorRig, data: SheetRows):  # type: ignore[no-untyped-def]
    return await rig.validator.validate(data)


# --- headers ----------------------------------------------------------------------------------


async def test_missing_required_headers_are_one_file_level_issue_listing_them() -> None:
    rig = make_rig()

    issues, summary, plan, keys = await _validate(rig, sheet(row(), drop=("Precio", "ML")))

    [issue] = issues
    assert (issue.row, issue.code) == (None, "CATALOG_IMPORT_HEADER_MISSING")
    assert issue.column == "ML, Precio"  # the header texts, in template order (deviation 2)
    assert (summary, plan, keys) == (None, None, [])


async def test_optional_headers_may_be_absent() -> None:
    rig = make_rig()
    optional = tuple(c for c in cells.COLUMNS if c not in cells.REQUIRED_COLUMNS)

    issues, summary, plan, _ = await _validate(rig, sheet(row(), drop=optional))

    assert issues == []
    assert summary is not None
    assert plan is not None


async def test_duplicated_headers_are_one_file_level_issue() -> None:
    rig = make_rig()
    data = sheet(row())
    data = SheetRows(rows=data.rows, headers=data.headers, duplicated=["precio", "marca"])

    issues, summary, plan, _ = await _validate(rig, data)

    [issue] = issues
    assert (issue.row, issue.code) == (None, "CATALOG_IMPORT_HEADER_DUPLICATED")
    assert issue.column == "Precio, Marca"
    assert (summary, plan) == (None, None)


async def test_missing_and_duplicated_headers_are_both_reported_and_stop_everything() -> None:
    rig = make_rig(downloads=BOTH)
    data = sheet(row(Imagen_1=URL_1), drop=("Precio",))
    data = SheetRows(rows=data.rows, headers=data.headers, duplicated=["marca"])

    issues, _, _, keys = await _validate(rig, data)

    assert _report(issues) == [
        (None, "Precio", "CATALOG_IMPORT_HEADER_MISSING"),
        (None, "Marca", "CATALOG_IMPORT_HEADER_DUPLICATED"),
    ]
    assert rig.references.existing_calls == []
    assert rig.downloader.calls == []
    assert keys == []


# --- a valid file -----------------------------------------------------------------------------


async def test_a_valid_row_gives_no_issues_a_summary_and_a_plan() -> None:
    rig = make_rig()

    issues, summary, plan, keys = await _validate(
        rig,
        sheet(
            row(
                Notas_de_salida="Bergamota, Limón",
                Notas_de_corazon="Rosa",
                Notas_de_fondo=" Ámbar ,Vainilla,",
                Descripcion="  Fresco y amaderado  ",
                Publicado="Sí",
            )
        ),
    )

    assert issues == []
    assert keys == []
    assert summary is not None
    assert (
        summary.perfumes_new,
        summary.perfumes_updated,
        summary.presentations_new,
        summary.presentations_updated,
        summary.photos_new,
    ) == (1, 0, 1, 0, 0)
    assert plan is not None
    [perfume] = plan.perfumes
    assert perfume.is_new is True
    assert (perfume.brand_id, perfume.concentration_id, perfume.family_id) == (
        BRAND.id,
        CONCENTRATION.id,
        FAMILY.id,
    )
    assert (perfume.name, perfume.gender, perfume.description) == (
        "Eros",
        Gender.MEN,
        "Fresco y amaderado",
    )
    assert perfume.top_notes == ("Bergamota", "Limón")
    assert perfume.heart_notes == ("Rosa",)
    assert perfume.base_notes == ("Ámbar", "Vainilla")
    assert (perfume.publish, perfume.first_row, perfume.photos) == (True, 2, ())
    [presentation] = perfume.presentations
    assert (presentation.presentation_id, presentation.ml, presentation.price_cents) == (
        None,
        100,
        250_000,
    )
    assert (presentation.availability, presentation.row) == ("in_stock", 2)
    assert presentation.sale_price_cents is None
    assert (presentation.lead_time_min_days, presentation.lead_time_max_days) == (None, None)


async def test_every_new_perfume_gets_its_own_new_id() -> None:
    rig = make_rig()

    _, _, plan, _ = await _validate(rig, sheet(row(), row(Nombre="Dylan Blue")))

    assert plan is not None
    first, second = plan.perfumes
    assert first.perfume_id != second.perfume_id


async def test_a_blank_published_cell_leaves_the_flag_as_it_is() -> None:
    rig = make_rig()

    _, _, plan, _ = await _validate(rig, sheet(row(), row(Nombre="Bleu", Publicado="No")))

    assert plan is not None
    assert [perfume.publish for perfume in plan.perfumes] == [None, False]


async def test_text_cells_may_be_numbers_and_a_concentration_may_be_its_abbreviation() -> None:
    rig = make_rig()

    issues, _, plan, _ = await _validate(rig, sheet(row(Nombre=212, Concentración="edt")))

    assert issues == []
    assert plan is not None
    assert plan.perfumes[0].name == "212"
    assert plan.perfumes[0].concentration_id == CONCENTRATION.id


@pytest.mark.parametrize(
    "value", ["Eau de Toilette", "eau de toilette", "EAU DE TOILETTE", "Eau  de  Toilétte", "EDT"]
)
async def test_a_concentration_matches_by_name_slug_or_abbreviation_ignoring_case(
    value: str,
) -> None:
    rig = make_rig()

    issues, _, plan, _ = await _validate(rig, sheet(row(Concentración=value)))

    assert issues == []
    assert plan is not None
    assert plan.perfumes[0].concentration_id == CONCENTRATION.id


async def test_a_name_slug_wins_over_an_abbreviation_slug() -> None:
    clash = CONCENTRATION_B.__class__(
        id=UUID(int=40), slug="edt", alt_slug=None, name="EDT", is_active=True
    )
    rig = make_rig(concentrations=[CONCENTRATION, clash])

    _, _, plan, _ = await _validate(rig, sheet(row(Concentración="edt")))

    assert plan is not None
    assert plan.perfumes[0].concentration_id == clash.id  # the name slug, not CONCENTRATION's EDT


@pytest.mark.parametrize("column", ["Marca", "Familia olfativa"])
async def test_brands_and_families_match_by_slug_ignoring_case_and_accents(column: str) -> None:
    rig = make_rig()
    value = "VERSACE" if column == "Marca" else "AROMATICA"

    issues, _, _, _ = await _validate(rig, sheet(row(**{column: value})))

    assert issues == []


# --- required cells and unknown references ----------------------------------------------------


@pytest.mark.parametrize("column", cells.REQUIRED_COLUMNS)
@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_an_empty_required_cell_is_required_on_its_row_and_column(
    column: str, blank: str | None
) -> None:
    rig = make_rig()

    issues, summary, plan, _ = await _validate(
        rig, sheet(row(), row(**{"Nombre": "Bleu", column: blank}))
    )

    assert [i for i in issues if i.code == "CATALOG_IMPORT_REQUIRED"] == [
        ImportIssue(3, column, "CATALOG_IMPORT_REQUIRED", "This cell is required")
    ]
    assert (summary, plan) == (None, None)


async def test_every_issue_of_a_row_is_collected_not_only_the_first() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig, sheet(row(Marca="Nadie", Familia_olfativa="Nada", ML="abc", Precio="10.555"))
    )

    assert _report(issues) == [
        (2, "Marca", "CATALOG_IMPORT_BRAND_UNKNOWN"),
        (2, "Familia olfativa", "CATALOG_IMPORT_FAMILY_UNKNOWN"),
        (2, "ML", "CATALOG_IMPORT_VALUE_INVALID"),
        (2, "Precio", "CATALOG_IMPORT_VALUE_INVALID"),
    ]


async def test_issues_are_sorted_by_row_then_template_column_order() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig,
        sheet(
            row(Precio="x", Marca="Nadie"),
            row(Nombre="Dylan Blue", Género="Otro", Marca="Nadie"),
        ),
    )

    assert _report(issues) == [
        (2, "Marca", "CATALOG_IMPORT_BRAND_UNKNOWN"),
        (2, "Precio", "CATALOG_IMPORT_VALUE_INVALID"),
        (3, "Marca", "CATALOG_IMPORT_BRAND_UNKNOWN"),
        (3, "Género", "CATALOG_IMPORT_VALUE_INVALID"),
    ]


async def test_an_unknown_concentration_is_reported_on_its_column() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(Concentración="Extrait")))

    assert _report(issues) == [(2, "Concentración", "CATALOG_IMPORT_CONCENTRATION_UNKNOWN")]


async def test_an_unknown_reference_row_is_not_grouped_so_no_stored_perfume_is_looked_up() -> None:
    rig = make_rig()

    await _validate(rig, sheet(row(Marca="Nadie")))

    assert rig.references.existing_calls == []


# --- cell values through the domain value objects ---------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "column", "code"),
    [
        ({"ML": "abc"}, "ML", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"ML": 10.5}, "ML", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"ML": 0}, "ML", "CATALOG_PRESENTATION_ML_INVALID"),
        ({"ML": 1001}, "ML", "CATALOG_PRESENTATION_ML_INVALID"),
        ({"Precio": "10.555"}, "Precio", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"Precio": -5}, "Precio", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"Precio": 0}, "Precio", "CATALOG_PRESENTATION_PRICE_INVALID"),
        ({"Precio": 100_001}, "Precio", "CATALOG_PRESENTATION_PRICE_INVALID"),
        ({"Género": "Otro"}, "Género", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"Disponibilidad": "Agotado"}, "Disponibilidad", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"Publicado": "Quizá"}, "Publicado", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"Nombre": "!!!"}, "Nombre", "CATALOG_PERFUME_NAME_INVALID"),
        ({"Nombre": "x" * 81}, "Nombre", "CATALOG_PERFUME_NAME_INVALID"),
        ({"Descripción": "d" * 2001}, "Descripción", "CATALOG_PERFUME_DESCRIPTION_TOO_LONG"),
        ({"Descripción": "d" * 4001}, "Descripción", "CATALOG_IMPORT_VALUE_INVALID"),
        (
            {"Notas_de_salida": ",".join("n" * 11 for _ in range(11))},
            "Notas de salida",
            "CATALOG_PERFUME_NOTES_INVALID",
        ),
        ({"Notas_de_corazon": "n" * 41}, "Notas de corazón", "CATALOG_PERFUME_NOTES_INVALID"),
        (
            {"Notas_de_fondo": ",".join(["n"] * 11)},
            "Notas de fondo",
            "CATALOG_PERFUME_NOTES_INVALID",
        ),
        ({"Imagen_1": "ftp://x.test/a.jpg"}, "Imagen 1", "CATALOG_IMPORT_IMAGE_URL_INVALID"),
        ({"Imagen_2": "no-scheme.test/a.jpg"}, "Imagen 2", "CATALOG_IMPORT_IMAGE_URL_INVALID"),
        (
            {"Imagen_3": "https://x.test/" + "a" * 2000},
            "Imagen 3",
            "CATALOG_IMPORT_IMAGE_URL_INVALID",
        ),
        ({"Entrega_min_dias": "abc"}, "Entrega mín. (días)", "CATALOG_IMPORT_VALUE_INVALID"),
        ({"Entrega_max_dias": 2.5}, "Entrega máx. (días)", "CATALOG_IMPORT_VALUE_INVALID"),
    ],
)
async def test_an_invalid_cell_is_one_issue_on_its_row_and_column(
    overrides: dict[str, cells.Cell], column: str, code: str
) -> None:
    rig = make_rig(downloads=BOTH)

    issues, summary, plan, _ = await _validate(rig, sheet(row(**overrides)))

    assert _report(issues) == [(2, column, code)]
    assert (summary, plan) == (None, None)
    assert rig.downloader.calls == []  # a group with issues downloads nothing


async def test_ten_notes_of_forty_characters_per_level_are_accepted() -> None:
    rig = make_rig()
    notes = ",".join(f"{'n' * 39}{i}" for i in range(10))

    issues, _, plan, _ = await _validate(rig, sheet(row(Notas_de_salida=notes)))

    assert issues == []
    assert plan is not None
    assert len(plan.perfumes[0].top_notes) == 10


# --- sale prices and windows (decision 66) ----------------------------------------------------


async def test_a_sale_with_dates_becomes_a_cancun_window_in_the_plan() -> None:
    rig = make_rig()

    issues, _, plan, _ = await _validate(
        rig, sheet(row(Precio_oferta=1999.99, Oferta_desde="01/11/2026", Oferta_hasta="30/11/2026"))
    )

    assert issues == []
    assert plan is not None
    [presentation] = plan.perfumes[0].presentations
    assert presentation.sale_price_cents == 199_999
    assert presentation.sale_starts_at is not None
    assert presentation.sale_ends_at is not None
    assert presentation.sale_starts_at.isoformat() == "2026-11-01T00:00:00-05:00"
    assert presentation.sale_ends_at.isoformat() == "2026-12-01T00:00:00-05:00"


async def test_a_sale_without_dates_is_a_sale_without_a_window() -> None:
    rig = make_rig()

    _, _, plan, _ = await _validate(rig, sheet(row(Precio_oferta=2000)))

    assert plan is not None
    [presentation] = plan.perfumes[0].presentations
    assert presentation.sale_price_cents == 200_000
    assert (presentation.sale_starts_at, presentation.sale_ends_at) == (None, None)


async def test_a_sale_with_only_one_date_has_an_open_ended_window() -> None:
    rig = make_rig()

    _, _, plan, _ = await _validate(
        rig,
        sheet(row(Precio_oferta=2000, Oferta_hasta=cells.sale_day("30/11/2026").value)),  # type: ignore[union-attr]
    )

    assert plan is not None
    [presentation] = plan.perfumes[0].presentations
    assert presentation.sale_starts_at is None
    assert presentation.sale_ends_at is not None
    assert presentation.sale_ends_at.isoformat() == "2026-12-01T00:00:00-05:00"


@pytest.mark.parametrize("sale", [2500, 2500.01, 9999])
async def test_a_sale_price_not_below_the_price_is_invalid_on_the_sale_column(
    sale: float,
) -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(Precio_oferta=sale)))

    assert _report(issues) == [(2, "Precio oferta", "CATALOG_PRESENTATION_SALE_INVALID")]


async def test_a_sale_price_one_cent_below_the_price_is_valid() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(Precio_oferta=2499.99)))

    assert issues == []


async def test_a_sale_that_ends_before_it_starts_is_invalid() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig, sheet(row(Precio_oferta=2000, Oferta_desde="30/11/2026", Oferta_hasta="01/11/2026"))
    )

    assert _report(issues) == [(2, "Precio oferta", "CATALOG_PRESENTATION_SALE_INVALID")]


async def test_a_sale_window_without_a_sale_price_is_invalid() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(Oferta_desde="01/11/2026")))

    assert _report(issues) == [(2, "Precio oferta", "CATALOG_PRESENTATION_SALE_INVALID")]


@pytest.mark.parametrize("column", ["Oferta_desde", "Oferta_hasta"])
async def test_a_malformed_sale_date_is_a_value_error_on_its_own_column(column: str) -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(Precio_oferta=2000, **{column: "mañana"})))

    assert _report(issues) == [
        (2, column.replace("_", " "), "CATALOG_IMPORT_VALUE_INVALID"),
    ]


# --- availability -----------------------------------------------------------------------------


async def test_made_to_order_with_lead_times_is_planned_with_them() -> None:
    rig = make_rig()

    issues, _, plan, _ = await _validate(
        rig, sheet(row(Disponibilidad="Sobre pedido", Entrega_min_dias=3, Entrega_max_dias="5"))
    )

    assert issues == []
    assert plan is not None
    [presentation] = plan.perfumes[0].presentations
    assert presentation.availability == "made_to_order"
    assert (presentation.lead_time_min_days, presentation.lead_time_max_days) == (3, 5)


@pytest.mark.parametrize(
    "lead",
    [
        {},
        {"Entrega_min_dias": 3},
        {"Entrega_max_dias": 5},
        {"Entrega_min_dias": 6, "Entrega_max_dias": 5},
        {"Entrega_min_dias": 0, "Entrega_max_dias": 5},
        {"Entrega_min_dias": 1, "Entrega_max_dias": 91},
    ],
)
async def test_made_to_order_needs_both_lead_times_in_range_and_ordered(
    lead: dict[str, cells.Cell],
) -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(Disponibilidad="Sobre pedido", **lead)))

    assert _report(issues) == [(2, "Disponibilidad", "CATALOG_PRESENTATION_AVAILABILITY_INVALID")]


@pytest.mark.parametrize(
    "lead",
    [
        {"Entrega_min_dias": 3},
        {"Entrega_max_dias": 5},
        {"Entrega_min_dias": 3, "Entrega_max_dias": 5},
    ],
)
async def test_in_stock_needs_both_lead_times_empty(lead: dict[str, cells.Cell]) -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(**lead)))

    assert _report(issues) == [(2, "Disponibilidad", "CATALOG_PRESENTATION_AVAILABILITY_INVALID")]


# --- archived references (decision 23) --------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "column", "code"),
    [
        ({"Marca": "Chanel"}, "Marca", "CATALOG_IMPORT_BRAND_ARCHIVED"),
        ({"Concentración": "Colonia"}, "Concentración", "CATALOG_IMPORT_CONCENTRATION_ARCHIVED"),
        ({"Concentración": "COL"}, "Concentración", "CATALOG_IMPORT_CONCENTRATION_ARCHIVED"),
        ({"Familia_olfativa": "Cítrica"}, "Familia olfativa", "CATALOG_IMPORT_FAMILY_ARCHIVED"),
    ],
)
async def test_a_new_perfume_needs_an_active_brand_family_and_concentration(
    overrides: dict[str, cells.Cell], column: str, code: str
) -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(**overrides)))

    assert _report(issues) == [(2, column, code)]


async def test_an_existing_perfume_keeps_its_archived_brand_and_concentration() -> None:
    key = identity(BRAND_ARCHIVED, "eros", CONCENTRATION_ARCHIVED)
    rig = make_rig(perfumes={key: existing()})

    issues, summary, plan, _ = await _validate(
        rig, sheet(row(Marca="Chanel", Concentración="Colonia"))
    )

    assert issues == []
    assert summary is not None
    assert summary.perfumes_updated == 1
    assert plan is not None
    assert plan.perfumes[0].perfume_id == EXISTING_ID


async def test_an_existing_perfume_keeps_an_archived_family_it_already_has() -> None:
    rig = make_rig(perfumes={identity(): existing(family=FAMILY_ARCHIVED)})

    issues, _, _, _ = await _validate(rig, sheet(row(Familia_olfativa="Cítrica")))

    assert issues == []


async def test_an_existing_perfume_cannot_move_to_an_archived_family() -> None:
    rig = make_rig(perfumes={identity(): existing(family=FAMILY)})

    issues, _, _, _ = await _validate(rig, sheet(row(Familia_olfativa="Cítrica")))

    assert _report(issues) == [(2, "Familia olfativa", "CATALOG_IMPORT_FAMILY_ARCHIVED")]


async def test_an_existing_perfume_may_move_to_another_active_family() -> None:
    rig = make_rig(perfumes={identity(): existing(family=FAMILY)})

    issues, _, plan, _ = await _validate(rig, sheet(row(Familia_olfativa="Floral")))

    assert issues == []
    assert plan is not None
    assert plan.perfumes[0].family_id == FAMILY_B.id


async def test_an_archived_perfume_is_reported_on_every_row_of_its_group() -> None:
    rig = make_rig(perfumes={identity(): existing(archived=True)})

    issues, _, _, _ = await _validate(rig, sheet(row(ML=50), row(ML=100), row(Nombre="Otro")))

    assert _report(issues) == [
        (2, "Nombre", "CATALOG_IMPORT_PERFUME_ARCHIVED"),
        (3, "Nombre", "CATALOG_IMPORT_PERFUME_ARCHIVED"),
    ]


async def test_an_archived_presentation_is_reported_on_its_ml_only() -> None:
    rig = make_rig(
        perfumes={
            identity(): existing(
                presentations={
                    50: (ARCHIVED_PRESENTATION, False),
                    100: (ACTIVE_PRESENTATION, True),
                }
            )
        }
    )

    issues, _, _, _ = await _validate(rig, sheet(row(ML=100), row(ML=50), row(ML=30)))

    assert _report(issues) == [(3, "ML", "CATALOG_IMPORT_PRESENTATION_ARCHIVED")]


# --- grouping, mismatch and duplicates (decisions 61, 68) -------------------------------------


async def test_rows_of_one_perfume_become_one_perfume_with_a_presentation_each() -> None:
    rig = make_rig()

    issues, summary, plan, _ = await _validate(
        rig, sheet(row(ML=100), row(ML=50, Precio=1500, Nombre="  eros "))
    )

    assert issues == []
    assert plan is not None
    [perfume] = plan.perfumes
    assert [(p.ml, p.price_cents, p.row) for p in perfume.presentations] == [
        (100, 250_000, 2),
        (50, 150_000, 3),
    ]
    assert summary is not None
    assert (summary.perfumes_new, summary.presentations_new) == (1, 2)


async def test_name_case_and_accents_do_not_split_a_perfume() -> None:
    rig = make_rig()

    _, _, plan, _ = await _validate(
        rig, sheet(row(Nombre="Éros", ML=100), row(Nombre="EROS", ML=50))
    )

    assert plan is not None
    assert len(plan.perfumes) == 1


async def test_another_concentration_or_brand_is_another_perfume() -> None:
    rig = make_rig()

    _, summary, plan, _ = await _validate(
        rig,
        sheet(row(), row(Concentración="Eau de Parfum"), row(Marca="Dior")),
    )

    assert plan is not None
    assert [p.brand_id for p in plan.perfumes] == [BRAND.id, BRAND.id, BRAND_B.id]
    assert [p.concentration_id for p in plan.perfumes] == [
        CONCENTRATION.id,
        CONCENTRATION_B.id,
        CONCENTRATION.id,
    ]
    assert summary is not None
    assert summary.perfumes_new == 3


async def test_the_plan_lists_perfumes_in_the_order_of_their_first_row() -> None:
    rig = make_rig()

    _, _, plan, _ = await _validate(
        rig, sheet(row(Nombre="Bleu"), row(Nombre="Azzaro"), row(Nombre="Bleu", ML=50))
    )

    assert plan is not None
    assert [(p.name, p.first_row) for p in plan.perfumes] == [("Bleu", 2), ("Azzaro", 3)]


@pytest.mark.parametrize(
    ("column", "first", "other"),
    [
        ("Género", "Hombre", "Mujer"),
        ("Familia olfativa", "Aromática", "Floral"),
        ("Descripción", "Una", "Otra"),
        ("Notas de salida", "A,B", "A,C"),
        ("Notas de corazón", "A", "B"),
        ("Notas de fondo", "A", "B"),
        ("Publicado", "Sí", "No"),
        ("Imagen 1", URL_1, URL_2),
        ("Imagen 2", URL_1, URL_2),
        ("Imagen 3", URL_1, URL_2),
    ],
)
async def test_a_later_row_that_differs_on_a_perfume_column_is_a_mismatch_on_that_column(
    column: str, first: cells.Cell, other: cells.Cell
) -> None:
    rig = make_rig(downloads=BOTH)

    issues, _, _, _ = await _validate(
        rig, sheet(row(**{column: first}), row(ML=50, **{column: other}))
    )

    # Only the later row is blamed: the first row is the reference.
    assert _report(issues) == [(3, column, "CATALOG_IMPORT_PERFUME_MISMATCH")]
    assert rig.downloader.calls == []


async def test_a_mismatch_names_the_column_with_its_header_text() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig, sheet(row(Descripcion="Una"), row(ML=50, Descripcion="Otra"))
    )

    assert _report(issues) == [(3, "Descripción", "CATALOG_IMPORT_PERFUME_MISMATCH")]


async def test_each_differing_column_is_its_own_mismatch() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig,
        sheet(
            row(Descripcion="Una"),
            row(ML=50, Descripcion="Otra", Género="Mujer", Notas_de_fondo="Cedro"),
        ),
    )

    assert _report(issues) == [
        (3, "Género", "CATALOG_IMPORT_PERFUME_MISMATCH"),
        (3, "Notas de fondo", "CATALOG_IMPORT_PERFUME_MISMATCH"),
        (3, "Descripción", "CATALOG_IMPORT_PERFUME_MISMATCH"),
    ]


async def test_the_comparison_ignores_case_in_words_and_spacing_in_notes() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig,
        sheet(
            row(Género="Hombre", Notas_de_salida="A, B", Publicado="Sí"),
            row(ML=50, Género="hombre", Notas_de_salida=" A,B ,", Publicado="si"),
        ),
    )

    assert issues == []


async def test_a_blank_perfume_cell_differs_from_a_filled_one() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(
        rig, sheet(row(Descripcion="Una"), row(ML=50, Descripcion=None))
    )

    assert _report(issues) == [(3, "Descripción", "CATALOG_IMPORT_PERFUME_MISMATCH")]


async def test_a_repeated_ml_is_a_duplicate_on_the_later_row_only() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(ML=100), row(ML=50), row(ML="100")))

    assert _report(issues) == [(4, "ML", "CATALOG_IMPORT_DUPLICATE_ROW")]


async def test_the_same_ml_in_two_perfumes_is_not_a_duplicate() -> None:
    rig = make_rig()

    issues, _, _, _ = await _validate(rig, sheet(row(ML=100), row(Nombre="Bleu", ML=100)))

    assert issues == []


async def test_stored_perfumes_are_looked_up_once_for_all_groups() -> None:
    rig = make_rig()

    await _validate(rig, sheet(row(), row(ML=50), row(Nombre="Bleu"), row(Marca="Dior")))

    [call] = rig.references.existing_calls
    assert sorted(call) == sorted([identity(), identity(name_slug="bleu"), identity(BRAND_B)])


async def test_the_summary_counts_updated_perfumes_and_presentations_and_carries_their_ids() -> (
    None
):
    rig = make_rig(
        perfumes={identity(): existing(presentations={100: (ACTIVE_PRESENTATION, True)})}
    )

    issues, summary, plan, _ = await _validate(
        rig, sheet(row(ML=100), row(ML=50), row(Nombre="Nuevo"))
    )

    assert issues == []
    assert summary is not None
    assert (
        summary.perfumes_new,
        summary.perfumes_updated,
        summary.presentations_new,
        summary.presentations_updated,
    ) == (1, 1, 2, 1)
    assert plan is not None
    updated, new = plan.perfumes
    assert (updated.is_new, updated.perfume_id) == (False, EXISTING_ID)
    assert [(p.ml, p.presentation_id) for p in updated.presentations] == [
        (100, ACTIVE_PRESENTATION),
        (50, None),
    ]
    assert new.is_new is True


# --- images: placement (decision 58) ----------------------------------------------------------


async def test_a_new_perfume_stages_each_image_at_its_final_keys_in_order() -> None:
    rig = make_rig(downloads=BOTH)

    issues, summary, plan, keys = await _validate(
        rig, sheet(row(Imagen_1=URL_1, Imagen_2=URL_2, Imagen_3=URL_3))
    )

    assert issues == []
    assert plan is not None
    [perfume] = plan.perfumes
    assert [(p.position, p.source_url, p.replaces_photo_id) for p in perfume.photos] == [
        (1, URL_1, None),
        (2, URL_2, None),
        (3, URL_3, None),
    ]
    for photo in perfume.photos:
        card, detail = photo_keys(perfume.perfume_id, photo.photo_id)
        assert (photo.card_key, photo.detail_key) == (card, detail)
        assert rig.storage.objects[card] == IMAGE.card
        assert rig.storage.objects[detail] == IMAGE.detail
        assert rig.storage.content_types[card] == "image/webp"
        assert (photo.width, photo.height) == (1200, 900)
    assert sorted(keys) == sorted(plan.staged_keys())
    assert len(rig.storage.objects) == 6
    assert summary is not None
    assert summary.photos_new == 3


async def test_an_image_of_an_existing_perfume_is_staged_under_its_stored_id() -> None:
    rig = make_rig(downloads=BOTH, perfumes={identity(): existing()})

    _, _, plan, keys = await _validate(rig, sheet(row(Imagen_1=URL_1)))

    assert plan is not None
    [photo] = plan.perfumes[0].photos
    assert photo.card_key.startswith(f"perfumes/{EXISTING_ID}/")
    assert keys == [photo.card_key, photo.detail_key]


async def test_an_image_cell_that_is_blank_stages_nothing() -> None:
    rig = make_rig(downloads=BOTH)

    _, summary, plan, keys = await _validate(rig, sheet(row(Imagen_1="  ", Imagen_2=None)))

    assert rig.downloader.calls == []
    assert keys == []
    assert plan is not None
    assert plan.perfumes[0].photos == ()
    assert summary is not None
    assert summary.photos_new == 0


async def test_the_same_url_as_the_stored_photo_downloads_nothing_and_is_not_counted() -> None:
    rig = make_rig(
        downloads=BOTH,
        perfumes={identity(): existing(photos={1: (OLD_PHOTO, URL_1)})},
    )

    issues, summary, plan, keys = await _validate(rig, sheet(row(Imagen_1=URL_1)))

    assert issues == []
    assert rig.downloader.calls == []
    assert rig.images.calls == []
    assert keys == []
    assert plan is not None
    assert plan.perfumes[0].photos == ()
    assert summary is not None
    assert summary.photos_new == 0


async def test_a_different_url_at_a_stored_position_replaces_that_photo_in_place() -> None:
    rig = make_rig(
        downloads=BOTH,
        perfumes={identity(): existing(photos={1: (OLD_PHOTO, URL_3), 2: (OLD_PHOTO_2, URL_2)})},
    )

    _, summary, plan, _ = await _validate(rig, sheet(row(Imagen_1=URL_1, Imagen_2=URL_2)))

    assert plan is not None
    [photo] = plan.perfumes[0].photos  # position 2 has the same URL: skipped
    assert (photo.position, photo.replaces_photo_id, photo.source_url) == (1, OLD_PHOTO, URL_1)
    assert photo.photo_id != OLD_PHOTO
    assert rig.downloader.calls == [URL_1]
    assert summary is not None
    assert summary.photos_new == 1  # replacements are counted (deviation 8)


async def test_a_stored_photo_from_the_panel_has_no_source_url_so_a_url_replaces_it() -> None:
    rig = make_rig(downloads=BOTH, perfumes={identity(): existing(photos={1: (OLD_PHOTO, None)})})

    _, _, plan, _ = await _validate(rig, sheet(row(Imagen_1=URL_1)))

    assert plan is not None
    [photo] = plan.perfumes[0].photos
    assert (photo.position, photo.replaces_photo_id) == (1, OLD_PHOTO)


@pytest.mark.parametrize(
    ("stored", "cells_", "expected"),
    [
        # no stored photos: append in ascending N
        (0, {"Imagen_1": URL_1, "Imagen_2": URL_2}, [(1, None), (2, None)]),
        # N <= k replaces, N > k appends at the next free position
        (1, {"Imagen_2": URL_2, "Imagen_3": URL_3}, [(2, None), (3, None)]),
        (1, {"Imagen_3": URL_3}, [(2, None)]),  # not position 3: the next free one
        (2, {"Imagen_3": URL_3}, [(3, None)]),
        (2, {"Imagen_2": URL_2, "Imagen_3": URL_3}, [(2, OLD_PHOTO_2), (3, None)]),
        (3, {"Imagen_1": URL_1, "Imagen_3": URL_3}, [(1, OLD_PHOTO), (3, UUID(int=703))]),
    ],
)
async def test_photos_are_replaced_when_stored_and_appended_when_not(
    stored: int, cells_: dict[str, cells.Cell], expected: list[tuple[int, UUID | None]]
) -> None:
    old_ids = {1: OLD_PHOTO, 2: OLD_PHOTO_2, 3: UUID(int=703)}
    photos: dict[int, tuple[UUID, str | None]] = {
        n: (old_ids[n], f"https://old.test/{n}.jpg") for n in range(1, stored + 1)
    }
    rig = make_rig(downloads=BOTH, perfumes={identity(): existing(photos=photos)})

    issues, _, plan, _ = await _validate(rig, sheet(row(**cells_)))

    assert issues == []
    assert plan is not None
    assert [(p.position, p.replaces_photo_id) for p in plan.perfumes[0].photos] == expected


async def test_only_the_first_row_images_are_used_and_the_others_must_agree() -> None:
    rig = make_rig(downloads=BOTH)

    _, _, plan, _ = await _validate(
        rig, sheet(row(ML=100, Imagen_1=URL_1), row(ML=50, Imagen_1=URL_1))
    )

    assert rig.downloader.calls == [URL_1]
    assert plan is not None
    assert len(plan.perfumes[0].photos) == 1


async def test_a_url_repeated_in_the_file_is_downloaded_and_processed_once() -> None:
    rig = make_rig(downloads=BOTH)

    _, summary, plan, keys = await _validate(
        rig, sheet(row(Imagen_1=URL_1, Imagen_2=URL_1), row(Nombre="Bleu", Imagen_1=URL_1))
    )

    assert rig.downloader.calls == [URL_1]
    assert rig.images.calls == [PNG]
    assert plan is not None
    photo_ids = [photo.photo_id for p in plan.perfumes for photo in p.photos]
    assert len(photo_ids) == 3
    assert len(set(photo_ids)) == 3  # each staged photo has its own id and keys
    assert len(set(keys)) == len(keys) == 6
    assert summary is not None
    assert summary.photos_new == 3


async def test_at_most_four_downloads_run_at_the_same_time() -> None:
    running = 0
    peak = 0

    class SlowDownloader:
        calls: list[str] = []

        async def fetch(self, url: str) -> Result[bytes, DomainError]:
            nonlocal running, peak
            self.calls.append(url)
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return Ok(b"x")

    rig = make_rig()
    rig.validator = ImportValidator(
        references=rig.references,
        downloader=SlowDownloader(),
        images=rig.images,
        storage=rig.storage,
        image_budget=timedelta(minutes=20),
    )
    rows = [row(Nombre=f"Perfume {n}", Imagen_1=f"https://img.test/{n}.jpg") for n in range(12)]

    issues, summary, _, _ = await rig.validator.validate(sheet(*rows))

    assert issues == []
    assert summary is not None
    assert summary.photos_new == 12
    assert peak == 4


# --- images: failures -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ImportImageUnreachable(details={"status": 404}), "CATALOG_IMPORT_IMAGE_UNREACHABLE"),
        (ImportImageHostBlocked(), "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"),
        (PhotoTooLarge(details={"max_bytes": 10}), "CATALOG_PHOTO_TOO_LARGE"),
    ],
)
async def test_a_download_error_is_an_issue_on_the_first_row_of_the_perfume_and_image_column(
    error: DomainError, code: str
) -> None:
    rig = make_rig(downloads={URL_1: PNG, URL_2: error})

    issues, summary, plan, keys = await _validate(
        rig,
        sheet(
            row(ML=100, Imagen_1=URL_1, Imagen_2=URL_2), row(ML=50, Imagen_1=URL_1, Imagen_2=URL_2)
        ),
    )

    assert _report(issues) == [(2, "Imagen 2", code)]
    assert (summary, plan) == (None, None)
    # The image that did work was staged anyway: the caller deletes the keys it gets back.
    assert len(keys) == 2
    assert set(keys) == set(rig.storage.objects)


async def test_a_file_that_is_not_an_image_is_the_processor_error_on_the_image_column() -> None:
    rig = make_rig(downloads={URL_1: b"<html>not an image</html>"})
    rig.images.errors[b"<html>not an image</html>"] = PhotoFormatInvalid(
        details={"formats": ["jpeg", "png", "webp"]}
    )

    issues, _, _, keys = await _validate(rig, sheet(row(Imagen_1=URL_1)))

    assert _report(issues) == [(2, "Imagen 1", "CATALOG_PHOTO_FORMAT_INVALID")]
    assert keys == []
    assert rig.storage.objects == {}


async def test_an_unreachable_url_not_in_the_downloader_is_unreachable() -> None:
    rig = make_rig(downloads={})

    issues, _, _, _ = await _validate(rig, sheet(row(Imagen_3="https://nowhere.test/a.jpg")))

    assert _report(issues) == [(2, "Imagen 3", "CATALOG_IMPORT_IMAGE_UNREACHABLE")]


async def test_one_failing_url_shared_by_two_perfumes_blames_both() -> None:
    rig = make_rig(downloads={URL_1: ImportImageHostBlocked()})

    issues, _, _, _ = await _validate(
        rig, sheet(row(Imagen_1=URL_1), row(Nombre="Bleu", Imagen_1=URL_1))
    )

    assert _report(issues) == [
        (2, "Imagen 1", "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"),
        (3, "Imagen 1", "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"),
    ]
    assert rig.downloader.calls == [URL_1]


async def test_a_group_with_issues_downloads_nothing_but_a_clean_group_still_stages() -> None:
    rig = make_rig(downloads=BOTH)

    issues, summary, plan, keys = await _validate(
        rig,
        sheet(
            row(Nombre="Malo", Precio="abc", Imagen_1=URL_1),
            row(Nombre="Bueno", Imagen_2=URL_2),
        ),
    )

    assert _report(issues) == [(2, "Precio", "CATALOG_IMPORT_VALUE_INVALID")]
    assert (summary, plan) == (None, None)
    assert rig.downloader.calls == [URL_2]
    assert len(keys) == 2
    assert set(keys) == set(rig.storage.objects)


async def test_a_mismatch_in_a_group_stops_its_downloads() -> None:
    rig = make_rig(downloads=BOTH)

    await _validate(rig, sheet(row(Imagen_1=URL_1), row(ML=50, Imagen_1=URL_2)))

    assert rig.downloader.calls == []


async def test_a_storage_failure_while_staging_deletes_what_was_uploaded_and_raises() -> None:
    rig = make_rig(downloads=BOTH)
    rig.storage.fail_puts = True

    with pytest.raises(ExceptionGroup) as raised:
        await rig.validator.validate(sheet(row(Imagen_1=URL_1)))

    assert raised.group_contains(RuntimeError, match="storage put failed")
    assert len(rig.storage.deleted) == 2  # deviation 4: the validator cleans up its own uploads
    assert rig.storage.objects == {}


async def test_a_failing_cleanup_does_not_hide_the_original_error() -> None:
    rig = make_rig(downloads=BOTH)
    rig.storage.fail_puts = True
    rig.storage.fail_deletes = True

    with pytest.raises(ExceptionGroup) as raised:
        await rig.validator.validate(sheet(row(Imagen_1=URL_1)))

    assert raised.group_contains(RuntimeError, match="storage put failed")


async def test_a_processor_exception_becomes_an_unreachable_issue_on_its_cell() -> None:
    # Repair round 1 (M1): an unexpected exception for one URL never fails the import.
    rig = make_rig(downloads=BOTH)

    async def boom(data: bytes):  # type: ignore[no-untyped-def]
        raise RuntimeError("pillow exploded")

    rig.images.process = boom  # type: ignore[method-assign]

    issues, _, _, keys = await _validate(rig, sheet(row(Imagen_1=URL_1)))

    assert _report(issues) == [(2, "Imagen 1", "CATALOG_IMPORT_IMAGE_UNREACHABLE")]
    assert keys == []


async def test_a_downloader_exception_becomes_an_unreachable_issue_on_its_cell() -> None:
    # Repair round 1 (M1 safety net): the fetch side, not only the processor.
    rig = make_rig(downloads=BOTH)

    async def boom(url: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("socket exploded")

    rig.downloader.fetch = boom  # type: ignore[method-assign]

    issues, _, _, keys = await _validate(rig, sheet(row(Imagen_1=URL_1)))

    assert _report(issues) == [(2, "Imagen 1", "CATALOG_IMPORT_IMAGE_UNREACHABLE")]
    assert keys == []


async def test_an_unexpected_exception_for_one_url_leaves_the_other_urls_staged() -> None:
    rig = make_rig(downloads=BOTH)
    real_fetch = rig.downloader.fetch

    async def fetch(url: str):  # type: ignore[no-untyped-def]
        if url == URL_1:
            raise RuntimeError("socket exploded")
        return await real_fetch(url)

    rig.downloader.fetch = fetch  # type: ignore[method-assign]

    issues, _, _, keys = await _validate(rig, sheet(row(Imagen_1=URL_1, Imagen_2=URL_2)))

    assert _report(issues) == [(2, "Imagen 1", "CATALOG_IMPORT_IMAGE_UNREACHABLE")]
    assert len(keys) == 2  # the second image was staged; the caller deletes it


# --- images: time budget (repair round 1, L4) ---------------------------------------------------


def _validator_with_budget(rig: ValidatorRig, budget: timedelta) -> ImportValidator:
    return ImportValidator(
        references=rig.references,
        downloader=rig.downloader,
        images=rig.images,
        storage=rig.storage,
        image_budget=budget,
    )


async def test_with_no_time_left_no_download_starts_and_every_image_is_unreachable() -> None:
    rig = make_rig(downloads=BOTH)
    validator = _validator_with_budget(rig, timedelta(0))

    issues, _, plan, keys = await validator.validate(sheet(row(Imagen_1=URL_1, Imagen_2=URL_2)))

    assert _report(issues) == [
        (2, "Imagen 1", "CATALOG_IMPORT_IMAGE_UNREACHABLE"),
        (2, "Imagen 2", "CATALOG_IMPORT_IMAGE_UNREACHABLE"),
    ]
    assert rig.downloader.calls == []
    assert rig.images.calls == []
    assert plan is None
    assert keys == []


async def test_a_download_whose_turn_comes_after_the_deadline_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fragancia_api.modules.catalog.application import import_validation

    # The deadline is set at 0 + 10 s; the first URL's turn comes at 5 s, the second's at 11 s.
    readings = iter([0.0, 5.0, 11.0])
    monkeypatch.setattr(
        import_validation, "time", SimpleNamespace(monotonic=lambda: next(readings))
    )
    rig = make_rig(downloads=BOTH)
    validator = _validator_with_budget(rig, timedelta(seconds=10))

    issues, _, _, keys = await validator.validate(sheet(row(Imagen_1=URL_1, Imagen_2=URL_2)))

    assert _report(issues) == [(2, "Imagen 2", "CATALOG_IMPORT_IMAGE_UNREACHABLE")]
    assert rig.downloader.calls == [URL_1]  # the late URL never reached the network
    assert len(keys) == 2  # the first image was staged and is returned for cleanup
