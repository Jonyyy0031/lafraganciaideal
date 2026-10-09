from datetime import UTC, datetime
from uuid import UUID

import pytest

from fragancia_api.modules.catalog.application.commands.presentations import (
    AddPresentation,
    ArchivePresentation,
    RestorePresentation,
    UpdatePresentation,
)
from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PresentationAlreadyExists,
)
from fragancia_api.modules.catalog.domain.perfume import Perfume
from fragancia_api.modules.catalog.infrastructure.in_memory_perfumes import InMemoryPerfumes
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Money, Ok, Result
from tests.unit.catalog.perfume_support import (
    World,
    add_presentation,
    error_code,
    make_perfume,
    presentation_request,
    unwrap_ok,
)

UNKNOWN_ID = UUID(int=404)
CLOCK_NOW = FixedClock().now()


class _SaveFails(InMemoryPerfumes):
    """Loads normally, but `save` reports the ml unique constraint (a concurrent admin)."""

    async def save(
        self, perfume: Perfume
    ) -> Result[None, PerfumeAlreadyExists | PresentationAlreadyExists]:
        return Err(PresentationAlreadyExists())


def _command[T](command: type[T], perfumes: InMemoryPerfumes) -> T:
    return command(  # type: ignore[call-arg]
        perfumes=perfumes, transactions=InMemoryTransactionRunner(), clock=FixedClock()
    )


def _world_with_perfume() -> tuple[World, Perfume]:
    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    return world, perfume


# --- add --------------------------------------------------------------------------------------


async def test_adds_an_active_presentation_stamped_with_the_clock() -> None:
    world, perfume = _world_with_perfume()
    future = datetime(2030, 1, 1, tzinfo=UTC)

    result = await _command(AddPresentation, world.perfumes).execute(
        perfume.id,
        presentation_request(
            ml=200,
            price_cents=390_000,
            sale_price_cents=350_000,
            sale_ends_at=future,
            availability="made_to_order",
            lead_time_min_days=7,
            lead_time_max_days=10,
        ),
    )

    presentation_id = unwrap_ok(result)
    [presentation] = world.perfumes.by_id[perfume.id].presentations
    assert presentation.id == presentation_id
    assert presentation.is_active
    assert presentation.created_at == CLOCK_NOW
    assert presentation.ml.value == 200
    assert presentation.price.amount == Money(390_000)
    assert presentation.sale is not None
    assert (presentation.sale.price, presentation.sale.starts_at, presentation.sale.ends_at) == (
        Money(350_000),
        None,
        future,
    )
    assert (
        presentation.availability.kind,
        presentation.availability.min_days,
        presentation.availability.max_days,
    ) == ("made_to_order", 7, 10)
    assert world.perfumes.by_id[perfume.id].updated_at == CLOCK_NOW


async def test_add_to_an_unknown_perfume_is_not_found() -> None:
    world = World()

    result = await _command(AddPresentation, world.perfumes).execute(
        UNKNOWN_ID, presentation_request()
    )

    assert error_code(result) == "CATALOG_PERFUME_NOT_FOUND"


async def test_add_refuses_a_repeated_ml_and_keeps_one_presentation() -> None:
    world, perfume = _world_with_perfume()
    add = _command(AddPresentation, world.perfumes)
    await add.execute(perfume.id, presentation_request(ml=100))

    result = await add.execute(perfume.id, presentation_request(ml=100, price_cents=999))

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert len(world.perfumes.by_id[perfume.id].presentations) == 1


async def test_add_is_refused_for_an_archived_perfume() -> None:
    world, perfume = _world_with_perfume()
    perfume.archive()

    result = await _command(AddPresentation, world.perfumes).execute(
        perfume.id, presentation_request()
    )

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"ml": 0}, "CATALOG_PRESENTATION_ML_INVALID"),
        ({"ml": 1001}, "CATALOG_PRESENTATION_ML_INVALID"),
        ({"price_cents": 0}, "CATALOG_PRESENTATION_PRICE_INVALID"),
        ({"price_cents": 10_000_001}, "CATALOG_PRESENTATION_PRICE_INVALID"),
        ({"sale_price_cents": 250_000}, "CATALOG_PRESENTATION_SALE_INVALID"),
        (
            {
                "sale_price_cents": 100,
                "sale_starts_at": datetime(2030, 1, 2, tzinfo=UTC),
                "sale_ends_at": datetime(2030, 1, 1, tzinfo=UTC),
            },
            "CATALOG_PRESENTATION_SALE_INVALID",
        ),
        (
            {"sale_starts_at": datetime(2030, 1, 2, tzinfo=UTC)},
            "CATALOG_PRESENTATION_SALE_INVALID",
        ),
        ({"availability": "made_to_order"}, "CATALOG_PRESENTATION_AVAILABILITY_INVALID"),
        ({"lead_time_min_days": 3}, "CATALOG_PRESENTATION_AVAILABILITY_INVALID"),
    ],
)
async def test_add_validates_the_values(overrides: dict[str, object], code: str) -> None:
    world, perfume = _world_with_perfume()

    result = await _command(AddPresentation, world.perfumes).execute(
        perfume.id, presentation_request(**overrides)
    )

    assert error_code(result) == code
    assert world.perfumes.by_id[perfume.id].presentations == []


async def test_add_validates_in_the_order_ml_price_sale_availability() -> None:
    world, perfume = _world_with_perfume()
    add = _command(AddPresentation, world.perfumes)
    everything_wrong = {
        "ml": 0,
        "price_cents": 0,
        "sale_price_cents": 9_999_999,
        "availability": "made_to_order",
    }

    ml = await add.execute(perfume.id, presentation_request(**everything_wrong))
    price = await add.execute(perfume.id, presentation_request(**{**everything_wrong, "ml": 5}))
    sale = await add.execute(
        perfume.id, presentation_request(**{**everything_wrong, "ml": 5, "price_cents": 100})
    )
    availability = await add.execute(
        perfume.id,
        presentation_request(
            ml=5, price_cents=100, sale_price_cents=50, availability="made_to_order"
        ),
    )

    assert error_code(ml) == "CATALOG_PRESENTATION_ML_INVALID"
    assert error_code(price) == "CATALOG_PRESENTATION_PRICE_INVALID"
    assert error_code(sale) == "CATALOG_PRESENTATION_SALE_INVALID"
    assert error_code(availability) == "CATALOG_PRESENTATION_AVAILABILITY_INVALID"


async def test_add_validates_the_values_before_looking_up_the_perfume() -> None:
    world = World()

    result = await _command(AddPresentation, world.perfumes).execute(
        UNKNOWN_ID, presentation_request(ml=0)
    )

    assert error_code(result) == "CATALOG_PRESENTATION_ML_INVALID"


async def test_add_returns_a_save_error() -> None:
    world, perfume = _world_with_perfume()
    failing = _SaveFails(world.brands, world.families, world.concentrations, perfume)

    result = await _command(AddPresentation, failing).execute(perfume.id, presentation_request())

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"


# --- update -----------------------------------------------------------------------------------


async def test_update_replaces_ml_price_sale_and_availability() -> None:
    world, perfume = _world_with_perfume()
    presentation_id = add_presentation(perfume, ml=100)
    perfume.archive_presentation(presentation_id)

    result = await _command(UpdatePresentation, world.perfumes).execute(
        perfume.id,
        presentation_id,
        presentation_request(
            ml=150,
            price_cents=300_000,
            sale_price_cents=1,
            availability="made_to_order",
            lead_time_min_days=1,
            lead_time_max_days=90,
        ),
    )

    assert isinstance(result, Ok)
    [presentation] = world.perfumes.by_id[perfume.id].presentations
    assert presentation.ml.value == 150
    assert presentation.price.amount == Money(300_000)
    assert presentation.sale is not None and presentation.sale.price == Money(1)
    assert presentation.availability.kind == "made_to_order"
    assert presentation.is_active is False  # an update never changes the state
    assert world.perfumes.by_id[perfume.id].updated_at == CLOCK_NOW


async def test_update_can_remove_the_sale() -> None:
    world, perfume = _world_with_perfume()
    add = _command(AddPresentation, world.perfumes)
    presentation_id = unwrap_ok(
        await add.execute(perfume.id, presentation_request(sale_price_cents=100))
    )

    result = await _command(UpdatePresentation, world.perfumes).execute(
        perfume.id, presentation_id, presentation_request()
    )

    assert isinstance(result, Ok)
    assert world.perfumes.by_id[perfume.id].presentations[0].sale is None


async def test_update_refuses_the_ml_of_another_presentation() -> None:
    world, perfume = _world_with_perfume()
    add_presentation(perfume, ml=100)
    second = add_presentation(perfume, ml=200)

    result = await _command(UpdatePresentation, world.perfumes).execute(
        perfume.id, second, presentation_request(ml=100)
    )

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"
    assert [p.ml.value for p in world.perfumes.by_id[perfume.id].presentations] == [100, 200]


async def test_update_unknown_perfume_or_presentation_is_not_found() -> None:
    world, perfume = _world_with_perfume()
    update = _command(UpdatePresentation, world.perfumes)

    unknown_perfume = await update.execute(UNKNOWN_ID, UNKNOWN_ID, presentation_request())
    unknown_presentation = await update.execute(perfume.id, UNKNOWN_ID, presentation_request())

    assert error_code(unknown_perfume) == "CATALOG_PERFUME_NOT_FOUND"
    assert error_code(unknown_presentation) == "CATALOG_PRESENTATION_NOT_FOUND"


async def test_update_validates_the_values_and_refuses_an_archived_perfume() -> None:
    world, perfume = _world_with_perfume()
    presentation_id = add_presentation(perfume)
    update = _command(UpdatePresentation, world.perfumes)

    invalid = await update.execute(perfume.id, presentation_id, presentation_request(ml=0))
    perfume.archive()
    archived = await update.execute(perfume.id, presentation_id, presentation_request())

    assert error_code(invalid) == "CATALOG_PRESENTATION_ML_INVALID"
    assert error_code(archived) == "CATALOG_PERFUME_ARCHIVED"


async def test_update_returns_a_save_error() -> None:
    world, perfume = _world_with_perfume()
    presentation_id = add_presentation(perfume)
    failing = _SaveFails(world.brands, world.families, world.concentrations, perfume)

    result = await _command(UpdatePresentation, failing).execute(
        perfume.id, presentation_id, presentation_request(price_cents=1)
    )

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"


# --- archive / restore ------------------------------------------------------------------------


async def test_archive_and_restore_are_idempotent() -> None:
    world, perfume = _world_with_perfume()
    presentation_id = add_presentation(perfume)
    archive = _command(ArchivePresentation, world.perfumes)
    restore = _command(RestorePresentation, world.perfumes)

    first = await archive.execute(perfume.id, presentation_id)
    second = await archive.execute(perfume.id, presentation_id)
    archived_state = world.perfumes.by_id[perfume.id].presentations[0].is_active
    third = await restore.execute(perfume.id, presentation_id)
    fourth = await restore.execute(perfume.id, presentation_id)

    assert all(isinstance(r, Ok) for r in (first, second, third, fourth))
    assert archived_state is False
    assert world.perfumes.by_id[perfume.id].presentations[0].is_active is True


async def test_archive_refuses_the_last_active_presentation_of_a_published_perfume() -> None:
    world, perfume = _world_with_perfume()
    first = add_presentation(perfume, ml=100)
    second = add_presentation(perfume, ml=200)
    perfume.publish(CLOCK_NOW)
    archive = _command(ArchivePresentation, world.perfumes)

    allowed = await archive.execute(perfume.id, second)
    refused = await archive.execute(perfume.id, first)

    assert isinstance(allowed, Ok)
    assert error_code(refused) == "CATALOG_PERFUME_LAST_PRESENTATION"
    assert world.perfumes.by_id[perfume.id].presentations[0].is_active


@pytest.mark.parametrize("command", [ArchivePresentation, RestorePresentation])
async def test_archive_and_restore_unknown_ids_are_not_found(command: type) -> None:
    world, perfume = _world_with_perfume()

    unknown_perfume = await _command(command, world.perfumes).execute(UNKNOWN_ID, UNKNOWN_ID)
    unknown_presentation = await _command(command, world.perfumes).execute(perfume.id, UNKNOWN_ID)

    assert error_code(unknown_perfume) == "CATALOG_PERFUME_NOT_FOUND"
    assert error_code(unknown_presentation) == "CATALOG_PRESENTATION_NOT_FOUND"


@pytest.mark.parametrize("command", [ArchivePresentation, RestorePresentation])
async def test_archive_and_restore_are_refused_for_an_archived_perfume(command: type) -> None:
    world, perfume = _world_with_perfume()
    presentation_id = add_presentation(perfume)
    perfume.archive()

    result = await _command(command, world.perfumes).execute(perfume.id, presentation_id)

    assert error_code(result) == "CATALOG_PERFUME_ARCHIVED"


@pytest.mark.parametrize("command", [ArchivePresentation, RestorePresentation])
async def test_archive_and_restore_return_a_save_error(command: type) -> None:
    world, perfume = _world_with_perfume()
    presentation_id = add_presentation(perfume)
    failing = _SaveFails(world.brands, world.families, world.concentrations, perfume)

    result = await _command(command, failing).execute(perfume.id, presentation_id)

    assert error_code(result) == "CATALOG_PRESENTATION_ALREADY_EXISTS"


# --- the in-memory adapter honors the SQL contract --------------------------------------------


async def test_an_unsaved_mutation_does_not_leak_into_the_store() -> None:
    world, perfume = _world_with_perfume()
    add_presentation(perfume)

    loaded = await world.perfumes.get_for_update(perfume.id)
    assert loaded is not None
    loaded.archive()
    loaded.presentations[0].is_active = False

    stored = world.perfumes.by_id[perfume.id]
    assert (stored.is_archived, stored.presentations[0].is_active) == (False, True)
