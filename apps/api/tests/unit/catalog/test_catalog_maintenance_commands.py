from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.application.commands.brand_status import (
    ArchiveBrand,
    RestoreBrand,
)
from fragancia_api.modules.catalog.application.commands.create_olfactory_family import (
    CreateOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.olfactory_family_status import (
    ArchiveOlfactoryFamily,
    RestoreOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.rename_brand import RenameBrand
from fragancia_api.modules.catalog.application.commands.rename_olfactory_family import (
    RenameOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.queries.list_olfactory_families import (
    ListAdminOlfactoryFamilies,
    ListPublicOlfactoryFamilies,
)
from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName, OlfactoryFamily
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryBrands,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, Result

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
UNKNOWN_ID = UUID(int=404)


def _brand(raw: str, *, active: bool = True) -> Brand:
    result = BrandName.create(raw)
    assert isinstance(result, Ok)
    brand = Brand.create(result.value, created_at=CREATED_AT)
    brand.is_active = active
    return brand


def _family(raw: str, *, active: bool = True) -> OlfactoryFamily:
    result = FamilyName.create(raw)
    assert isinstance(result, Ok)
    family = OlfactoryFamily.create(result.value, created_at=CREATED_AT)
    family.is_active = active
    return family


# --- brands -------------------------------------------------------------------------------


async def test_renames_a_brand_and_its_slug_follows() -> None:
    dior = _brand("Dior")
    store = InMemoryBrands(dior)

    result = await RenameBrand(brands=store, transactions=InMemoryTransactionRunner()).execute(
        dior.id, "  Christian   Dior "
    )

    assert isinstance(result, Ok)
    assert (store.by_id[dior.id].name.value, store.by_id[dior.id].slug) == (
        "Christian Dior",
        "christian-dior",
    )


async def test_renaming_a_brand_to_its_own_slug_succeeds() -> None:
    dior = _brand("Dior")
    store = InMemoryBrands(dior)

    result = await RenameBrand(brands=store, transactions=InMemoryTransactionRunner()).execute(
        dior.id, "DIOR"
    )

    assert isinstance(result, Ok)
    assert store.by_id[dior.id].name.value == "DIOR"


async def test_renaming_a_brand_to_another_brands_slug_is_a_conflict() -> None:
    dior, chanel = _brand("Dior"), _brand("Chanel")
    store = InMemoryBrands(dior, chanel)

    result = await RenameBrand(brands=store, transactions=InMemoryTransactionRunner()).execute(
        dior.id, "chanel"
    )

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"
    assert store.by_id[dior.id].name.value == "Dior"


async def test_renaming_a_brand_to_an_archived_brands_slug_is_a_conflict() -> None:
    dior, old = _brand("Dior"), _brand("Old Brand", active=False)
    store = InMemoryBrands(dior, old)

    result = await RenameBrand(brands=store, transactions=InMemoryTransactionRunner()).execute(
        dior.id, "old brand"
    )

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"


async def test_renaming_a_brand_with_an_invalid_name_is_rejected() -> None:
    dior = _brand("Dior")
    store = InMemoryBrands(dior)

    result = await RenameBrand(brands=store, transactions=InMemoryTransactionRunner()).execute(
        dior.id, "x"
    )

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_NAME_INVALID"
    assert store.by_id[dior.id].name.value == "Dior"


async def test_renaming_an_unknown_brand_is_not_found() -> None:
    result = await RenameBrand(
        brands=InMemoryBrands(), transactions=InMemoryTransactionRunner()
    ).execute(UNKNOWN_ID, "Dior")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_NOT_FOUND"


async def test_rename_passes_on_a_conflict_found_when_saving() -> None:
    """A concurrent rename slips past the exists check; `save` reports it and it is returned."""

    class RacingBrands(InMemoryBrands):
        async def exists_with_slug(self, slug: str, *, except_id: UUID | None = None) -> bool:
            return False  # the check saw nothing

        async def save(self, brand: Brand) -> Result[None, BrandAlreadyExists]:
            return Err(BrandAlreadyExists())

    dior = _brand("Dior")

    result = await RenameBrand(
        brands=RacingBrands(dior), transactions=InMemoryTransactionRunner()
    ).execute(dior.id, "Chanel")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_BRAND_ALREADY_EXISTS"


async def test_archives_and_restores_a_brand_idempotently() -> None:
    dior = _brand("Dior")
    store = InMemoryBrands(dior)
    transactions = InMemoryTransactionRunner()
    archive = ArchiveBrand(brands=store, transactions=transactions)
    restore = RestoreBrand(brands=store, transactions=transactions)

    assert isinstance(await archive.execute(dior.id), Ok)
    assert isinstance(await archive.execute(dior.id), Ok)
    assert store.by_id[dior.id].is_active is False
    assert isinstance(await restore.execute(dior.id), Ok)
    assert isinstance(await restore.execute(dior.id), Ok)
    assert store.by_id[dior.id].is_active is True


async def test_archiving_or_restoring_an_unknown_brand_is_not_found() -> None:
    store, transactions = InMemoryBrands(), InMemoryTransactionRunner()

    for command in (
        ArchiveBrand(brands=store, transactions=transactions),
        RestoreBrand(brands=store, transactions=transactions),
    ):
        result = await command.execute(UNKNOWN_ID)
        assert isinstance(result, Err)
        assert result.error.code == "CATALOG_BRAND_NOT_FOUND"


# --- olfactory families ---------------------------------------------------------------------


def _create(store: InMemoryOlfactoryFamilies) -> CreateOlfactoryFamily:
    return CreateOlfactoryFamily(
        families=store, transactions=InMemoryTransactionRunner(), clock=FixedClock()
    )


async def test_creates_an_active_family() -> None:
    store = InMemoryOlfactoryFamilies()

    result = await _create(store).execute("  Especiada ")

    assert isinstance(result, Ok)
    family = store.by_id[result.value]
    assert (family.name.value, family.slug, family.is_active) == ("Especiada", "especiada", True)
    assert family.created_at == FixedClock().now()


async def test_a_family_name_with_the_same_slug_is_a_conflict() -> None:
    store = InMemoryOlfactoryFamilies(_family("Cítrica"))

    result = await _create(store).execute("citrica")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_FAMILY_ALREADY_EXISTS"
    assert len(store.by_id) == 1


async def test_re_adding_an_archived_family_is_a_conflict() -> None:
    store = InMemoryOlfactoryFamilies(_family("Chipre", active=False))

    result = await _create(store).execute("Chipre")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_FAMILY_ALREADY_EXISTS"


async def test_an_invalid_family_name_creates_nothing() -> None:
    store = InMemoryOlfactoryFamilies()

    result = await _create(store).execute("x")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_FAMILY_NAME_INVALID"
    assert store.by_id == {}


async def test_renames_a_family_and_its_slug_follows() -> None:
    family = _family("Especiada")
    store = InMemoryOlfactoryFamilies(family)

    result = await RenameOlfactoryFamily(
        families=store, transactions=InMemoryTransactionRunner()
    ).execute(family.id, "Especiada Cálida")

    assert isinstance(result, Ok)
    assert store.by_id[family.id].slug == "especiada-calida"


async def test_renaming_a_family_to_its_own_slug_succeeds() -> None:
    family = _family("Floral")
    store = InMemoryOlfactoryFamilies(family)

    result = await RenameOlfactoryFamily(
        families=store, transactions=InMemoryTransactionRunner()
    ).execute(family.id, "FLORAL")

    assert isinstance(result, Ok)


async def test_renaming_a_family_to_another_familys_slug_is_a_conflict() -> None:
    floral, chipre = _family("Floral"), _family("Chipre")
    store = InMemoryOlfactoryFamilies(floral, chipre)

    result = await RenameOlfactoryFamily(
        families=store, transactions=InMemoryTransactionRunner()
    ).execute(floral.id, "chipre")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_FAMILY_ALREADY_EXISTS"


@pytest.mark.parametrize(
    ("family_id", "name", "code"),
    [
        (UNKNOWN_ID, "Floral", "CATALOG_FAMILY_NOT_FOUND"),
        (None, "x", "CATALOG_FAMILY_NAME_INVALID"),
    ],
)
async def test_renaming_a_family_rejects_unknown_ids_and_invalid_names(
    family_id: UUID | None, name: str, code: str
) -> None:
    existing = _family("Gourmand")
    store = InMemoryOlfactoryFamilies(existing)

    result = await RenameOlfactoryFamily(
        families=store, transactions=InMemoryTransactionRunner()
    ).execute(family_id or existing.id, name)

    assert isinstance(result, Err)
    assert result.error.code == code


async def test_archives_and_restores_a_family_idempotently() -> None:
    family = _family("Floral")
    store = InMemoryOlfactoryFamilies(family)
    transactions = InMemoryTransactionRunner()
    archive = ArchiveOlfactoryFamily(families=store, transactions=transactions)
    restore = RestoreOlfactoryFamily(families=store, transactions=transactions)

    assert isinstance(await archive.execute(family.id), Ok)
    assert isinstance(await archive.execute(family.id), Ok)
    assert store.by_id[family.id].is_active is False
    assert isinstance(await restore.execute(family.id), Ok)
    assert isinstance(await restore.execute(family.id), Ok)
    assert store.by_id[family.id].is_active is True


async def test_archiving_or_restoring_an_unknown_family_is_not_found() -> None:
    store, transactions = InMemoryOlfactoryFamilies(), InMemoryTransactionRunner()

    for command in (
        ArchiveOlfactoryFamily(families=store, transactions=transactions),
        RestoreOlfactoryFamily(families=store, transactions=transactions),
    ):
        result = await command.execute(UNKNOWN_ID)
        assert isinstance(result, Err)
        assert result.error.code == "CATALOG_FAMILY_NOT_FOUND"


async def test_public_family_list_has_active_families_ordered_by_name() -> None:
    store = InMemoryOlfactoryFamilies(
        _family("Zeta"), _family("armada"), _family("Chipre", active=False)
    )

    listed = await ListPublicOlfactoryFamilies(store).execute()

    assert [f.name for f in listed] == ["armada", "Zeta"]


async def test_admin_family_list_is_paginated_and_includes_archived() -> None:
    store = InMemoryOlfactoryFamilies(
        _family("C fam"), _family("A fam"), _family("B fam", active=False)
    )

    page = await ListAdminOlfactoryFamilies(store).execute(page=2, size=2)

    assert (page.total, page.page, page.size) == (3, 2, 2)
    assert [f.name for f in page.items] == ["C fam"]
