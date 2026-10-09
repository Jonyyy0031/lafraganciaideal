from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.application.commands.concentration_status import (
    ArchiveConcentration,
    RestoreConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_concentration import (
    CreateConcentration,
)
from fragancia_api.modules.catalog.application.commands.update_concentration import (
    UpdateConcentration,
)
from fragancia_api.modules.catalog.application.queries.list_concentrations import (
    ListAdminConcentrations,
    ListPublicConcentrations,
)
from fragancia_api.modules.catalog.domain.concentration import (
    Abbreviation,
    Concentration,
    ConcentrationName,
)
from fragancia_api.modules.catalog.domain.errors import (
    ConcentrationAbbreviationTaken,
    ConcentrationAlreadyExists,
)
from fragancia_api.modules.catalog.infrastructure.in_memory import InMemoryConcentrations
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
UNKNOWN_ID = UUID(int=404)


def _concentration(name: str, abbreviation: str, *, active: bool = True) -> Concentration:
    parsed_name = ConcentrationName.create(name)
    parsed_abbreviation = Abbreviation.create(abbreviation)
    assert isinstance(parsed_name, Ok) and isinstance(parsed_abbreviation, Ok)
    concentration = Concentration.create(
        parsed_name.value, parsed_abbreviation.value, created_at=CREATED_AT
    )
    concentration.is_active = active
    return concentration


def _create(store: InMemoryConcentrations) -> CreateConcentration:
    return CreateConcentration(
        concentrations=store, transactions=InMemoryTransactionRunner(), clock=FixedClock()
    )


def _update(store: InMemoryConcentrations) -> UpdateConcentration:
    return UpdateConcentration(concentrations=store, transactions=InMemoryTransactionRunner())


def _error_code(result: Result[object, object]) -> str:
    assert isinstance(result, Err)
    assert isinstance(result.error, DomainError)
    return str(result.error.code)


class _RacingConcentrations(InMemoryConcentrations):
    """The exists checks see nothing; `add` and `save` report a taken abbreviation."""

    async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
        return False

    async def exists_with_abbreviation(
        self, abbreviation_slug: str, *, except_id: UUID | None = None
    ) -> bool:
        return False

    async def add(
        self, concentration: Concentration
    ) -> Result[None, ConcentrationAlreadyExists | ConcentrationAbbreviationTaken]:
        return Err(ConcentrationAbbreviationTaken())

    async def save(
        self, concentration: Concentration
    ) -> Result[None, ConcentrationAlreadyExists | ConcentrationAbbreviationTaken]:
        return Err(ConcentrationAbbreviationTaken())


# --- create -----------------------------------------------------------------------------------


async def test_creates_an_active_concentration() -> None:
    store = InMemoryConcentrations()

    result = await _create(store).execute(" Body  Mist ", "Mist")

    assert isinstance(result, Ok)
    stored = store.by_id[result.value]
    assert (stored.name.value, stored.slug) == ("Body Mist", "body-mist")
    assert (stored.abbreviation.value, stored.abbreviation_slug) == ("Mist", "mist")
    assert stored.is_active
    assert stored.created_at == FixedClock().now()


async def test_a_name_with_the_same_slug_is_a_conflict() -> None:
    store = InMemoryConcentrations(_concentration("Eau de Toilette", "EDT"))

    result = await _create(store).execute("eau de toilette", "X1")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert len(store.by_id) == 1


async def test_an_abbreviation_with_the_same_slug_is_a_conflict() -> None:
    store = InMemoryConcentrations(_concentration("Eau de Toilette", "EDT"))

    result = await _create(store).execute("Toilette Fraîche", "edt")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    assert len(store.by_id) == 1


async def test_a_clash_on_both_name_and_abbreviation_reports_the_name_first() -> None:
    store = InMemoryConcentrations(_concentration("Eau de Toilette", "EDT"))

    result = await _create(store).execute("EAU DE TOILETTE", "edt")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ALREADY_EXISTS"


async def test_re_adding_an_archived_concentration_is_a_conflict() -> None:
    store = InMemoryConcentrations(_concentration("Parfum", "Parfum", active=False))

    by_name = await _create(store).execute("Parfum", "Other")
    by_abbreviation = await _create(store).execute("Other", "PARFUM")

    assert _error_code(by_name) == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert _error_code(by_abbreviation) == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"


@pytest.mark.parametrize(
    ("name", "abbreviation", "code"),
    [
        ("x", "EDT", "CATALOG_CONCENTRATION_NAME_INVALID"),
        ("Eau de Toilette", "X", "CATALOG_CONCENTRATION_ABBREVIATION_INVALID"),
        ("Eau de Toilette", "a" * 13, "CATALOG_CONCENTRATION_ABBREVIATION_INVALID"),
        # both invalid: the name is validated first
        ("x", "y", "CATALOG_CONCENTRATION_NAME_INVALID"),
    ],
)
async def test_an_invalid_text_creates_nothing(name: str, abbreviation: str, code: str) -> None:
    store = InMemoryConcentrations()

    result = await _create(store).execute(name, abbreviation)

    assert _error_code(result) == code
    assert store.by_id == {}


async def test_create_passes_on_a_conflict_found_when_adding() -> None:
    """A concurrent create slips past the exists checks; `add` reports it and it is returned."""
    result = await _create(_RacingConcentrations()).execute("Body Mist", "Mist")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"


# --- update -----------------------------------------------------------------------------------


async def test_updates_both_texts_and_the_slugs_follow() -> None:
    edt = _concentration("Eau de Toilette", "EDT")
    store = InMemoryConcentrations(edt)

    result = await _update(store).execute(edt.id, "  Body   Mist ", "Mist")

    assert isinstance(result, Ok)
    stored = store.by_id[edt.id]
    assert (stored.name.value, stored.slug) == ("Body Mist", "body-mist")
    assert (stored.abbreviation.value, stored.abbreviation_slug) == ("Mist", "mist")


async def test_updating_keeps_its_own_slugs_without_a_conflict() -> None:
    edt = _concentration("Eau de Toilette", "EDT")
    store = InMemoryConcentrations(edt)

    same_slugs = await _update(store).execute(edt.id, "EAU DE TOILETTE", "edt")
    only_abbreviation = await _update(store).execute(edt.id, "Eau de Toilette", "ET")

    assert isinstance(same_slugs, Ok) and isinstance(only_abbreviation, Ok)
    assert (store.by_id[edt.id].name.value, store.by_id[edt.id].abbreviation.value) == (
        "Eau de Toilette",
        "ET",
    )


async def test_updating_to_another_concentrations_name_is_a_conflict() -> None:
    edt, edp = _concentration("Eau de Toilette", "EDT"), _concentration("Eau de Parfum", "EDP")
    store = InMemoryConcentrations(edt, edp)

    result = await _update(store).execute(edt.id, "eau de parfum", "EDT")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert store.by_id[edt.id].name.value == "Eau de Toilette"


async def test_updating_to_another_concentrations_abbreviation_is_a_conflict() -> None:
    edt, edp = _concentration("Eau de Toilette", "EDT"), _concentration("Eau de Parfum", "EDP")
    store = InMemoryConcentrations(edt, edp)

    result = await _update(store).execute(edt.id, "Eau de Toilette", "edp")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"
    assert store.by_id[edt.id].abbreviation.value == "EDT"


async def test_updating_into_an_archived_concentrations_slugs_is_a_conflict() -> None:
    edt = _concentration("Eau de Toilette", "EDT")
    old = _concentration("Old Mist", "OM", active=False)
    store = InMemoryConcentrations(edt, old)

    by_name = await _update(store).execute(edt.id, "old mist", "EDT")
    by_abbreviation = await _update(store).execute(edt.id, "Eau de Toilette", "om")

    assert _error_code(by_name) == "CATALOG_CONCENTRATION_ALREADY_EXISTS"
    assert _error_code(by_abbreviation) == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"


async def test_an_update_clashing_on_both_reports_the_name_first() -> None:
    edt, edp = _concentration("Eau de Toilette", "EDT"), _concentration("Eau de Parfum", "EDP")
    store = InMemoryConcentrations(edt, edp)

    result = await _update(store).execute(edt.id, "Eau de Parfum", "EDP")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ALREADY_EXISTS"


@pytest.mark.parametrize(
    ("name", "abbreviation", "code"),
    [
        ("x", "EDT", "CATALOG_CONCENTRATION_NAME_INVALID"),
        ("Eau de Toilette", "X", "CATALOG_CONCENTRATION_ABBREVIATION_INVALID"),
    ],
)
async def test_updating_with_an_invalid_text_changes_nothing(
    name: str, abbreviation: str, code: str
) -> None:
    edt = _concentration("Eau de Toilette", "EDT")
    store = InMemoryConcentrations(edt)

    result = await _update(store).execute(edt.id, name, abbreviation)

    assert _error_code(result) == code
    assert (store.by_id[edt.id].name.value, store.by_id[edt.id].abbreviation.value) == (
        "Eau de Toilette",
        "EDT",
    )


async def test_updating_an_unknown_concentration_is_not_found() -> None:
    result = await _update(InMemoryConcentrations()).execute(UNKNOWN_ID, "Body Mist", "Mist")

    assert _error_code(result) == "CATALOG_CONCENTRATION_NOT_FOUND"


async def test_an_invalid_text_is_reported_before_an_unknown_id() -> None:
    result = await _update(InMemoryConcentrations()).execute(UNKNOWN_ID, "x", "Mist")

    assert _error_code(result) == "CATALOG_CONCENTRATION_NAME_INVALID"


async def test_update_passes_on_a_conflict_found_when_saving() -> None:
    """A concurrent update slips past the exists checks; `save` reports it and it is returned."""
    edt = _concentration("Eau de Toilette", "EDT")

    result = await _update(_RacingConcentrations(edt)).execute(edt.id, "Eau de Toilette", "EDP")

    assert _error_code(result) == "CATALOG_CONCENTRATION_ABBREVIATION_TAKEN"


# --- archive / restore ------------------------------------------------------------------------


async def test_archives_and_restores_a_concentration_idempotently() -> None:
    edt = _concentration("Eau de Toilette", "EDT")
    store, transactions = InMemoryConcentrations(edt), InMemoryTransactionRunner()
    archive = ArchiveConcentration(concentrations=store, transactions=transactions)
    restore = RestoreConcentration(concentrations=store, transactions=transactions)

    assert isinstance(await archive.execute(edt.id), Ok)
    assert isinstance(await archive.execute(edt.id), Ok)
    assert store.by_id[edt.id].is_active is False
    assert isinstance(await restore.execute(edt.id), Ok)
    assert isinstance(await restore.execute(edt.id), Ok)
    assert store.by_id[edt.id].is_active is True


async def test_archiving_or_restoring_an_unknown_concentration_is_not_found() -> None:
    store, transactions = InMemoryConcentrations(), InMemoryTransactionRunner()

    for command in (
        ArchiveConcentration(concentrations=store, transactions=transactions),
        RestoreConcentration(concentrations=store, transactions=transactions),
    ):
        assert _error_code(await command.execute(UNKNOWN_ID)) == "CATALOG_CONCENTRATION_NOT_FOUND"


# --- queries ----------------------------------------------------------------------------------


async def test_public_list_has_active_concentrations_ordered_by_name() -> None:
    store = InMemoryConcentrations(
        _concentration("Zeta", "ZZ"),
        _concentration("armada", "AR"),
        _concentration("Old", "OL", active=False),
    )

    listed = await ListPublicConcentrations(store).execute()

    assert [(c.name, c.abbreviation, c.slug) for c in listed] == [
        ("armada", "AR", "armada"),
        ("Zeta", "ZZ", "zeta"),
    ]


async def test_admin_list_is_paginated_and_includes_archived() -> None:
    store = InMemoryConcentrations(
        _concentration("C conc", "CC"),
        _concentration("A conc", "AA"),
        _concentration("B conc", "BB", active=False),
    )

    page = await ListAdminConcentrations(store).execute(page=2, size=2)

    assert (page.total, page.page, page.size) == (3, 2, 2)
    assert [c.name for c in page.items] == ["C conc"]
