from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.application.commands.perfumes import (
    ArchivePerfume,
    CreatePerfume,
    HidePerfume,
    PublishPerfume,
    RestorePerfume,
    UpdatePerfume,
)
from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PresentationAlreadyExists,
)
from fragancia_api.modules.catalog.domain.perfume import Perfume
from fragancia_api.modules.catalog.infrastructure.in_memory_perfumes import InMemoryPerfumes
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, Result
from tests.unit.catalog.perfume_support import (
    World,
    add_presentation,
    error_code,
    error_details,
    make_brand,
    make_concentration,
    make_family,
    make_perfume,
    unwrap_ok,
)

UNKNOWN_ID = UUID(int=404)
CLOCK_NOW = FixedClock().now()


class _Racing(InMemoryPerfumes):
    """The identity check sees nothing; `add` and `save` report a conflict (a concurrent admin)."""

    async def exists_with_identity(
        self,
        brand_id: UUID,
        name_slug: str,
        concentration_id: UUID,
        *,
        except_id: UUID | None = None,
    ) -> bool:
        return False

    async def add(self, perfume: Perfume) -> Result[None, PerfumeAlreadyExists]:
        return Err(PerfumeAlreadyExists())

    async def save(
        self, perfume: Perfume
    ) -> Result[None, PerfumeAlreadyExists | PresentationAlreadyExists]:
        return Err(PerfumeAlreadyExists())


def _create(world: World, perfumes: InMemoryPerfumes | None = None) -> CreatePerfume:
    return CreatePerfume(
        perfumes=perfumes or world.perfumes,
        brands=world.brands,
        families=world.families,
        concentrations=world.concentrations,
        transactions=InMemoryTransactionRunner(),
        clock=FixedClock(),
    )


def _update(world: World, perfumes: InMemoryPerfumes | None = None) -> UpdatePerfume:
    return UpdatePerfume(
        perfumes=perfumes or world.perfumes,
        brands=world.brands,
        families=world.families,
        concentrations=world.concentrations,
        transactions=InMemoryTransactionRunner(),
        clock=FixedClock(),
    )


def _status[T](command: type[T], perfumes: InMemoryPerfumes) -> T:
    return command(  # type: ignore[call-arg]
        perfumes=perfumes, transactions=InMemoryTransactionRunner(), clock=FixedClock()
    )


def _seed(world: World, name: str = "Eros") -> Perfume:
    return world.seed(make_perfume(world.brand, world.family, world.concentration, name=name))


# --- create -----------------------------------------------------------------------------------


async def test_creates_a_hidden_perfume_with_the_computed_slug() -> None:
    world = World()

    result = await _create(world).execute(
        world.request(
            name="  Eros  ",
            description="  Fresh ",
            top_notes=[" Mint "],
            heart_notes=["Apple"],
            base_notes=["Vanilla"],
        )
    )

    perfume = world.perfumes.by_id[unwrap_ok(result)]
    assert perfume.slug == "versace-eros-edt"
    assert (perfume.name.value, perfume.description.value) == ("Eros", "Fresh")
    assert (perfume.notes.top, perfume.notes.heart, perfume.notes.base) == (
        ("Mint",),
        ("Apple",),
        ("Vanilla",),
    )
    assert (perfume.is_published, perfume.is_archived) == (False, False)
    assert perfume.first_published_at is None
    assert (perfume.created_at, perfume.updated_at) == (CLOCK_NOW, CLOCK_NOW)
    assert perfume.presentations == []


async def test_rejects_the_same_identity_ignoring_case_and_spacing() -> None:
    world = World()
    await _create(world).execute(world.request(name="Eros"))

    result = await _create(world).execute(world.request(name="  EROS "))

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"
    assert len(world.perfumes.by_id) == 1


async def test_the_same_name_with_another_concentration_is_a_new_perfume() -> None:
    world = World()
    edp = make_concentration("Eau de Parfum", "EDP")
    world.concentrations.by_id[edp.id] = edp
    await _create(world).execute(world.request())

    result = await _create(world).execute(world.request(concentration_id=edp.id))

    assert world.perfumes.by_id[unwrap_ok(result)].slug == "versace-eros-edp"


async def test_the_same_name_with_another_brand_is_a_new_perfume() -> None:
    world = World()
    other = make_brand("Paco Rabanne")
    world.brands.by_id[other.id] = other
    await _create(world).execute(world.request())

    result = await _create(world).execute(world.request(brand_id=other.id))

    assert world.perfumes.by_id[unwrap_ok(result)].slug == "paco-rabanne-eros-edt"


async def test_a_perfume_that_is_archived_still_blocks_its_identity() -> None:
    world = World()
    _seed(world).archive()

    result = await _create(world).execute(world.request())

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"


async def test_validates_in_the_order_name_description_notes() -> None:
    world = World()
    too_long = "a" * 2001

    name_first = await _create(world).execute(
        world.request(name="x", description=too_long, top_notes=[""])
    )
    description_second = await _create(world).execute(
        world.request(description=too_long, top_notes=[""])
    )
    notes_third = await _create(world).execute(world.request(top_notes=[""]))

    assert error_code(name_first) == "CATALOG_PERFUME_NAME_INVALID"
    assert error_code(description_second) == "CATALOG_PERFUME_DESCRIPTION_TOO_LONG"
    assert error_code(notes_third) == "CATALOG_PERFUME_NOTES_INVALID"
    assert world.perfumes.by_id == {}


async def test_values_are_validated_before_the_references_are_read() -> None:
    world = World()

    result = await _create(world).execute(world.request(name="x", brand_id=UNKNOWN_ID))

    assert error_code(result) == "CATALOG_PERFUME_NAME_INVALID"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"brand_id": UNKNOWN_ID}, "CATALOG_PERFUME_BRAND_UNAVAILABLE"),
        ({"family_id": UNKNOWN_ID}, "CATALOG_PERFUME_FAMILY_UNAVAILABLE"),
        ({"concentration_id": UNKNOWN_ID}, "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"),
    ],
)
async def test_an_unknown_reference_is_unavailable(overrides: dict[str, UUID], code: str) -> None:
    world = World()

    result = await _create(world).execute(world.request(**overrides))

    assert error_code(result) == code
    assert world.perfumes.by_id == {}


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"brand": make_brand(active=False)}, "CATALOG_PERFUME_BRAND_UNAVAILABLE"),
        ({"family": make_family(active=False)}, "CATALOG_PERFUME_FAMILY_UNAVAILABLE"),
        (
            {"concentration": make_concentration(active=False)},
            "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE",
        ),
    ],
)
async def test_an_archived_reference_is_unavailable(kwargs: dict[str, object], code: str) -> None:
    world = World(**kwargs)  # type: ignore[arg-type]

    result = await _create(world).execute(world.request())

    assert error_code(result) == code
    assert world.perfumes.by_id == {}


async def test_references_are_checked_in_the_order_brand_family_concentration() -> None:
    world = World(make_brand(active=False), make_family(active=False), make_concentration())

    brand_first = await _create(world).execute(world.request())
    world.brand.is_active = True
    family_second = await _create(world).execute(world.request())
    world.family.is_active = True
    world.concentration.is_active = False
    concentration_last = await _create(world).execute(world.request())

    assert error_code(brand_first) == "CATALOG_PERFUME_BRAND_UNAVAILABLE"
    assert error_code(family_second) == "CATALOG_PERFUME_FAMILY_UNAVAILABLE"
    assert error_code(concentration_last) == "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"


async def test_reference_errors_come_before_the_identity_check() -> None:
    world = World()
    await _create(world).execute(world.request())
    world.brand.is_active = False

    result = await _create(world).execute(world.request())

    assert error_code(result) == "CATALOG_PERFUME_BRAND_UNAVAILABLE"


async def test_a_conflict_found_when_adding_is_returned() -> None:
    world = World()
    racing = _Racing(world.brands, world.families, world.concentrations)

    result = await _create(world, racing).execute(world.request())

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"


# --- update -----------------------------------------------------------------------------------


async def test_update_replaces_every_field_and_touches_updated_at() -> None:
    world = World()
    perfume = _seed(world)
    clock = FixedClock()
    clock.current = clock.now() + timedelta(days=1)
    command = UpdatePerfume(
        perfumes=world.perfumes,
        brands=world.brands,
        families=world.families,
        concentrations=world.concentrations,
        transactions=InMemoryTransactionRunner(),
        clock=clock,
    )

    result = await command.execute(
        perfume.id,
        world.request(
            name="Eros Flame",
            gender="unisex",
            description="New",
            top_notes=["Lemon"],
            heart_notes=["Rose"],
            base_notes=["Musk"],
        ),
    )

    assert isinstance(result, Ok)
    stored = world.perfumes.by_id[perfume.id]
    assert (stored.name.value, stored.slug, stored.gender.value) == (
        "Eros Flame",
        "versace-eros-flame-edt",
        "unisex",
    )
    assert stored.description.value == "New"
    assert (stored.notes.top, stored.notes.heart, stored.notes.base) == (
        ("Lemon",),
        ("Rose",),
        ("Musk",),
    )
    assert stored.updated_at == clock.now()
    assert stored.created_at == perfume.created_at


async def test_update_recomputes_the_slug_from_the_current_brand_name_and_abbreviation() -> None:
    world = World()
    perfume = _seed(world)
    assert perfume.slug == "versace-eros-edt"
    world.brand.rename(unwrap_ok(type(world.brand.name).create("Versace Home")))
    world.concentration.abbreviation = type(world.concentration.abbreviation)("EDT2")

    result = await _update(world).execute(perfume.id, world.request())

    assert isinstance(result, Ok)
    assert world.perfumes.by_id[perfume.id].slug == "versace-home-eros-edt2"


async def test_a_renamed_brand_does_not_change_the_slug_until_the_perfume_is_updated() -> None:
    world = World()
    perfume = _seed(world)

    world.brand.rename(unwrap_ok(type(world.brand.name).create("Versace Home")))

    assert world.perfumes.by_id[perfume.id].slug == "versace-eros-edt"


async def test_update_to_an_unknown_perfume_is_not_found() -> None:
    world = World()

    result = await _update(world).execute(UNKNOWN_ID, world.request())

    assert error_code(result) == "CATALOG_PERFUME_NOT_FOUND"


async def test_update_validates_values_before_looking_up_the_perfume() -> None:
    world = World()

    result = await _update(world).execute(UNKNOWN_ID, world.request(name="x"))

    assert error_code(result) == "CATALOG_PERFUME_NAME_INVALID"


async def test_update_may_keep_its_own_identity() -> None:
    world = World()
    perfume = _seed(world)

    result = await _update(world).execute(perfume.id, world.request(name="EROS"))

    assert isinstance(result, Ok)


async def test_update_refuses_the_identity_of_another_perfume() -> None:
    world = World()
    _seed(world, "Eros")
    other = _seed(world, "Eros Flame")

    result = await _update(world).execute(other.id, world.request(name="Eros"))

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"
    assert world.perfumes.by_id[other.id].name.value == "Eros Flame"


async def test_update_keeps_an_unchanged_archived_reference() -> None:
    world = World()
    perfume = _seed(world)
    world.brand.is_active = False
    world.family.is_active = False
    world.concentration.is_active = False

    result = await _update(world).execute(perfume.id, world.request(description="Still fine"))

    assert isinstance(result, Ok)
    stored = world.perfumes.by_id[perfume.id]
    assert stored.description.value == "Still fine"
    assert stored.slug == "versace-eros-edt"


async def test_update_to_an_active_family_when_the_current_one_is_archived() -> None:
    world = World()
    perfume = _seed(world)
    world.family.is_active = False
    fresh = make_family("Floral")
    world.families.by_id[fresh.id] = fresh

    result = await _update(world).execute(perfume.id, world.request(family_id=fresh.id))

    assert isinstance(result, Ok)
    assert world.perfumes.by_id[perfume.id].family_id == fresh.id


@pytest.mark.parametrize("reference", ["brand", "family", "concentration"])
async def test_update_to_an_archived_reference_is_refused(reference: str) -> None:
    world = World()
    perfume = _seed(world)
    brand = make_brand("Dior", active=False)
    family = make_family("Floral", active=False)
    concentration = make_concentration("Eau de Parfum", "EDP", active=False)
    world.brands.by_id[brand.id] = brand
    world.families.by_id[family.id] = family
    world.concentrations.by_id[concentration.id] = concentration
    replacement_id = {"brand": brand.id, "family": family.id, "concentration": concentration.id}[
        reference
    ]

    result = await _update(world).execute(
        perfume.id, world.request(**{f"{reference}_id": replacement_id})
    )

    assert error_code(result) == f"CATALOG_PERFUME_{reference.upper()}_UNAVAILABLE"
    assert world.perfumes.by_id[perfume.id].slug == "versace-eros-edt"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"brand_id": UNKNOWN_ID}, "CATALOG_PERFUME_BRAND_UNAVAILABLE"),
        ({"family_id": UNKNOWN_ID}, "CATALOG_PERFUME_FAMILY_UNAVAILABLE"),
        ({"concentration_id": UNKNOWN_ID}, "CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE"),
    ],
)
async def test_update_to_an_unknown_reference_is_unavailable(
    overrides: dict[str, UUID], code: str
) -> None:
    world = World()
    perfume = _seed(world)

    result = await _update(world).execute(perfume.id, world.request(**overrides))

    assert error_code(result) == code


async def test_updating_an_archived_perfume_is_refused() -> None:
    world = World()
    perfume = _seed(world)
    perfume = world.perfumes.by_id[perfume.id]
    perfume.archive()

    result = await _update(world).execute(perfume.id, world.request(name="Eros Flame"))

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"
    assert world.perfumes.by_id[perfume.id].name.value == "Eros"


async def test_an_archived_perfume_with_a_changed_archived_reference_reports_the_reference() -> (
    None
):
    """Plan 003, deviation 4: the references are checked before the aggregate refuses."""
    world = World()
    perfume = _seed(world)
    world.perfumes.by_id[perfume.id].archive()
    other = make_family("Floral", active=False)
    world.families.by_id[other.id] = other

    result = await _update(world).execute(perfume.id, world.request(family_id=other.id))

    assert error_code(result) == "CATALOG_PERFUME_FAMILY_UNAVAILABLE"


async def test_update_returns_a_conflict_found_when_saving() -> None:
    world = World()
    perfume = _seed(world)
    racing = _Racing(world.brands, world.families, world.concentrations, perfume)

    result = await _update(world, racing).execute(perfume.id, world.request(name="Eros Flame"))

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"


# --- status commands --------------------------------------------------------------------------


async def test_publish_shows_the_perfume_and_records_the_first_publication() -> None:
    world = World()
    perfume = _seed(world)
    add_presentation(world.perfumes.by_id[perfume.id])

    result = await _status(PublishPerfume, world.perfumes).execute(perfume.id)

    assert isinstance(result, Ok)
    stored = world.perfumes.by_id[perfume.id]
    assert stored.is_published
    assert stored.first_published_at == CLOCK_NOW


async def test_publish_is_refused_without_an_active_presentation() -> None:
    world = World()
    perfume = _seed(world)

    result = await _status(PublishPerfume, world.perfumes).execute(perfume.id)

    assert error_code(result) == "CATALOG_PERFUME_NOTHING_TO_SELL"
    assert not world.perfumes.by_id[perfume.id].is_published


async def test_publish_is_refused_for_an_archived_perfume() -> None:
    world = World()
    perfume = _seed(world)
    stored = world.perfumes.by_id[perfume.id]
    add_presentation(stored)
    stored.archive()

    result = await _status(PublishPerfume, world.perfumes).execute(perfume.id)

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"


async def test_hide_archive_and_restore_follow_the_aggregate() -> None:
    world = World()
    perfume = _seed(world)
    stored = world.perfumes.by_id[perfume.id]
    add_presentation(stored)
    await _status(PublishPerfume, world.perfumes).execute(perfume.id)

    hidden = await _status(HidePerfume, world.perfumes).execute(perfume.id)
    after_hide = (world.perfumes.by_id[perfume.id].is_published,)
    await _status(PublishPerfume, world.perfumes).execute(perfume.id)
    archived = await _status(ArchivePerfume, world.perfumes).execute(perfume.id)
    after_archive = (
        world.perfumes.by_id[perfume.id].is_archived,
        world.perfumes.by_id[perfume.id].is_published,
    )
    restored = await _status(RestorePerfume, world.perfumes).execute(perfume.id)
    after_restore = (
        world.perfumes.by_id[perfume.id].is_archived,
        world.perfumes.by_id[perfume.id].is_published,
    )

    assert isinstance(hidden, Ok) and isinstance(archived, Ok) and isinstance(restored, Ok)
    assert after_hide == (False,)
    assert after_archive == (True, False)
    assert after_restore == (False, False)


@pytest.mark.parametrize("command", [PublishPerfume, HidePerfume, ArchivePerfume, RestorePerfume])
async def test_status_commands_on_an_unknown_perfume_are_not_found(command: type) -> None:
    world = World()

    result = await _status(command, world.perfumes).execute(UNKNOWN_ID)

    assert error_code(result) == "CATALOG_PERFUME_NOT_FOUND"


@pytest.mark.parametrize("command", [HidePerfume, ArchivePerfume, RestorePerfume])
async def test_status_commands_return_a_save_error(command: type) -> None:
    world = World()
    perfume = _seed(world)
    racing = _Racing(world.brands, world.families, world.concentrations, perfume)

    result = await _status(command, racing).execute(perfume.id)

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"


async def test_publish_returns_a_save_error() -> None:
    world = World()
    perfume = make_perfume(world.brand, world.family, world.concentration)
    add_presentation(perfume)
    racing = _Racing(world.brands, world.families, world.concentrations, perfume)

    result = await _status(PublishPerfume, racing).execute(perfume.id)

    assert error_code(result) == "CATALOG_PERFUME_ALREADY_EXISTS"


async def test_error_details_of_a_validation_error_reach_the_caller() -> None:
    world = World()

    result = await _create(world).execute(world.request(top_notes=["a"] * 11))

    assert error_details(result) == {"max_per_level": 10, "max_length": 40}
