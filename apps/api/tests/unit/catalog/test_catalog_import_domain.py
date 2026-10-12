"""Plan 007: the `CatalogImport` aggregate, its transitions and the typed import plan."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.domain.catalog_import import (
    ISSUES_MAX,
    CatalogImport,
    ImportIssue,
    ImportPlan,
    ImportStatus,
    ImportSummary,
)
from fragancia_api.modules.catalog.domain.errors import ImportRequired
from tests.unit.catalog.import_support import PLAN, planned_perfume

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
TTL = timedelta(minutes=60)
SUMMARY = ImportSummary(1, 2, 3, 4, 5)
ALL_STATUSES = tuple(ImportStatus)


def _issue(row: int | None = 2) -> ImportIssue:
    return ImportIssue.of(ImportRequired(), row=row, column="Marca")


def _pending() -> CatalogImport:
    return CatalogImport.create(b"PK\x03\x04data", "admin-1", T0)


def _validating() -> CatalogImport:
    catalog_import = _pending()
    catalog_import.start(T1)
    return catalog_import


def _ready() -> CatalogImport:
    catalog_import = _validating()
    catalog_import.finish_ready(T2, TTL, 3, SUMMARY, PLAN)
    return catalog_import


# --- create -----------------------------------------------------------------------------------


def test_create_makes_a_pending_import_holding_the_file() -> None:
    catalog_import = _pending()

    assert catalog_import.status is ImportStatus.PENDING
    assert catalog_import.file == b"PK\x03\x04data"
    assert (catalog_import.created_by, catalog_import.created_at) == ("admin-1", T0)
    assert catalog_import.errors == ()
    assert catalog_import.plan is None
    assert catalog_import.summary is None


def test_each_import_gets_its_own_id() -> None:
    assert _pending().id != _pending().id


def test_the_eight_statuses_have_their_wire_values() -> None:
    assert [status.value for status in ImportStatus] == [
        "pending",
        "validating",
        "ready",
        "invalid",
        "failed",
        "expired",
        "applied",
        "discarded",
    ]


# --- start ------------------------------------------------------------------------------------


def test_start_moves_pending_to_validating_and_stamps_started_at() -> None:
    catalog_import = _pending()

    catalog_import.start(T1)

    assert catalog_import.status is ImportStatus.VALIDATING
    assert catalog_import.started_at == T1
    assert catalog_import.file is not None  # the file is dropped only when validation finishes


# --- finish_ready -----------------------------------------------------------------------------


def test_finish_ready_sets_expiry_summary_plan_and_drops_the_file() -> None:
    catalog_import = _validating()

    catalog_import.finish_ready(T2, TTL, 3, SUMMARY, PLAN)

    assert catalog_import.status is ImportStatus.READY
    assert catalog_import.expires_at == T2 + TTL
    assert catalog_import.finished_at == T2
    assert catalog_import.row_count == 3
    assert catalog_import.summary == SUMMARY
    assert catalog_import.plan == PLAN
    assert catalog_import.error_count == 0
    assert catalog_import.errors == ()
    assert catalog_import.file is None


# --- finish_invalid ---------------------------------------------------------------------------


def test_finish_invalid_keeps_the_issues_and_counts_them_and_drops_the_file() -> None:
    catalog_import = _validating()
    issues = [_issue(2), _issue(3)]

    catalog_import.finish_invalid(T2, 5, issues)

    assert catalog_import.status is ImportStatus.INVALID
    assert catalog_import.errors == tuple(issues)
    assert catalog_import.error_count == 2
    assert catalog_import.row_count == 5
    assert catalog_import.finished_at == T2
    assert catalog_import.file is None
    assert catalog_import.plan is None
    assert catalog_import.expires_at is None


def test_finish_invalid_without_a_row_count_is_a_file_level_report() -> None:
    catalog_import = _validating()

    catalog_import.finish_invalid(T2, None, [_issue(None)])

    assert catalog_import.row_count is None
    assert catalog_import.error_count == 1


def test_finish_invalid_keeps_exactly_1000_issues_when_given_1000() -> None:
    catalog_import = _validating()

    catalog_import.finish_invalid(T2, 2000, [_issue(n) for n in range(2, 1002)])

    assert (len(catalog_import.errors), catalog_import.error_count) == (1000, 1000)


def test_finish_invalid_caps_at_1000_issues_but_error_count_is_the_full_count() -> None:
    catalog_import = _validating()
    issues = [_issue(n) for n in range(2, 1003)]  # 1001 issues

    catalog_import.finish_invalid(T2, 2000, issues)

    assert ISSUES_MAX == 1000
    assert len(catalog_import.errors) == 1000
    assert catalog_import.errors == tuple(issues[:1000])  # the first ones, in order
    assert catalog_import.error_count == 1001


# --- fail -------------------------------------------------------------------------------------


@pytest.mark.parametrize("start", [False, True])
def test_fail_works_from_pending_and_from_validating(start: bool) -> None:
    catalog_import = _validating() if start else _pending()
    issue = _issue(None)

    catalog_import.fail(T2, issue)

    assert catalog_import.status is ImportStatus.FAILED
    assert catalog_import.errors == (issue,)
    assert catalog_import.error_count == 1
    assert catalog_import.finished_at == T2
    assert catalog_import.file is None


# --- expire -----------------------------------------------------------------------------------


def test_expire_returns_every_staged_key_drops_the_plan_and_expires() -> None:
    catalog_import = _ready()
    expected = [
        key
        for perfume in PLAN.perfumes
        for photo in perfume.photos
        for key in (photo.card_key, photo.detail_key)
    ]

    keys = catalog_import.expire()

    assert catalog_import.status is ImportStatus.EXPIRED
    assert catalog_import.plan is None
    assert keys == expected
    assert len(keys) == 6  # 3 photos in the plan, 2 renditions each


def test_expire_keeps_the_report_fields_of_a_ready_import() -> None:
    catalog_import = _ready()

    catalog_import.expire()

    assert catalog_import.summary == SUMMARY
    assert catalog_import.row_count == 3
    assert catalog_import.finished_at == T2


# --- transitions that are programming errors --------------------------------------------------

_ALLOWED: dict[str, set[ImportStatus]] = {
    "start": {ImportStatus.PENDING},
    "finish_ready": {ImportStatus.VALIDATING},
    "finish_invalid": {ImportStatus.VALIDATING},
    "fail": {ImportStatus.PENDING, ImportStatus.VALIDATING},
    "expire": {ImportStatus.READY},
}


def _call(catalog_import: CatalogImport, method: str) -> None:
    match method:
        case "start":
            catalog_import.start(T1)
        case "finish_ready":
            catalog_import.finish_ready(T2, TTL, 1, SUMMARY, PLAN)
        case "finish_invalid":
            catalog_import.finish_invalid(T2, 1, [_issue()])
        case "fail":
            catalog_import.fail(T2, _issue(None))
        case _:
            catalog_import.expire()


_FORBIDDEN = [
    (method, status)
    for method, allowed in _ALLOWED.items()
    for status in ALL_STATUSES
    if status not in allowed
]


@pytest.mark.parametrize(("method", "status"), _FORBIDDEN)
def test_every_transition_outside_the_table_raises_value_error_and_changes_nothing(
    method: str, status: ImportStatus
) -> None:
    catalog_import = CatalogImport(
        id=UUID(int=1), status=status, file=b"PK", created_by="a", created_at=T0
    )

    with pytest.raises(ValueError, match="expected one of"):
        _call(catalog_import, method)

    assert catalog_import.status is status
    assert catalog_import.file == b"PK"
    assert catalog_import.finished_at is None


# --- ImportIssue / ImportPlan -----------------------------------------------------------------


def test_an_issue_of_an_error_carries_its_code_and_message() -> None:
    issue = ImportIssue.of(ImportRequired(), row=7, column="Precio")

    assert (issue.row, issue.column) == (7, "Precio")
    assert issue.code == "CATALOG_IMPORT_REQUIRED"
    assert issue.message == "This cell is required"


def test_staged_keys_lists_card_then_detail_of_every_photo_in_plan_order() -> None:
    first = PLAN.perfumes[0]

    keys = PLAN.staged_keys()

    assert keys[:4] == [
        first.photos[0].card_key,
        first.photos[0].detail_key,
        first.photos[1].card_key,
        first.photos[1].detail_key,
    ]
    assert keys[4:] == [PLAN.perfumes[2].photos[0].card_key, PLAN.perfumes[2].photos[0].detail_key]


def test_an_empty_plan_stages_nothing() -> None:
    assert ImportPlan(perfumes=()).staged_keys() == []
    assert ImportPlan(perfumes=(planned_perfume(9, 0),)).staged_keys() == []
