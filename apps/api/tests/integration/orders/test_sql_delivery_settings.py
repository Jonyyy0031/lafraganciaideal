"""Delivery settings on PostgreSQL (`fragancia_test`): the repository, the reader, the table
constraints, the row lock and the use cases over the real adapters."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DataError, IntegrityError

from fragancia_api.container import Container
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.contracts import (
    DeliveryOptions,
    HomeDeliveryOptions,
    LocalDeliveryOptions,
    PickupOptions,
)
from fragancia_api.modules.orders.domain.delivery import DeliverySettings, PostalCode
from fragancia_api.modules.orders.infrastructure.sql_delivery_settings_repository import (
    SqlDeliverySettingsReader,
    SqlDeliverySettingsRepository,
)
from fragancia_api.modules.orders.infrastructure.tables import delivery_settings
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.orders.support import build_settings

pytestmark = pytest.mark.integration

TIMEOUT = 5
WAIT = 0.5


async def in_transaction[T](container: Container, work: Callable[[], Awaitable[T]]) -> T:
    async def unit() -> Result[T, DomainError]:
        return Ok(await work())

    result = await SqlTransactionRunner(container.database).run(unit)
    assert isinstance(result, Ok)
    value: T = result.value
    return value


async def row_count(container: Container) -> int:
    async with container.database.engine.connect() as connection:
        return int(
            (
                await connection.execute(text("SELECT count(*) FROM orders.delivery_settings"))
            ).scalar_one()
        )


def full_options() -> DeliveryOptions:
    return DeliveryOptions(
        home=HomeDeliveryOptions(enabled=True, price_cents=18000, free_from_cents=250000),
        local=LocalDeliveryOptions(
            enabled=True, price_cents=9000, postal_codes=["77505", "77500", "01000"]
        ),
        pickup=PickupOptions(enabled=True, address="Calle 1 #2, Cancún", hours="Lun-Vie 10-18"),
    )


# --- repository --------------------------------------------------------------------------------


async def test_the_seeded_default_row_reads_as_the_default_settings(container: Container) -> None:
    repo = SqlDeliverySettingsRepository(container.database)

    assert await in_transaction(container, repo.get) == DeliverySettings.default()
    assert await SqlDeliverySettingsReader(container.database).current() == (
        DeliverySettings.default()
    )


async def test_get_without_a_row_answers_the_default_and_writes_nothing(
    container: Container,
) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE orders.delivery_settings"))
    repo = SqlDeliverySettingsRepository(container.database)

    assert await in_transaction(container, repo.get) == DeliverySettings.default()
    assert await SqlDeliverySettingsReader(container.database).current() == (
        DeliverySettings.default()
    )
    assert await row_count(container) == 0


async def test_get_for_update_inserts_the_default_row_when_none_is_stored(
    container: Container,
) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE orders.delivery_settings"))
    repo = SqlDeliverySettingsRepository(container.database)

    settings = await in_transaction(container, repo.get_for_update)

    assert settings == DeliverySettings.default()
    assert await row_count(container) == 1


async def test_get_for_update_keeps_an_existing_row(container: Container) -> None:
    repo = SqlDeliverySettingsRepository(container.database)
    stored = build_settings()

    async def change() -> DeliverySettings:
        await repo.save(stored)
        return await repo.get_for_update()

    assert await in_transaction(container, change) == stored
    assert await row_count(container) == 1


async def test_saved_settings_round_trip_including_the_postal_code_array(
    container: Container,
) -> None:
    repo = SqlDeliverySettingsRepository(container.database)
    settings = build_settings(local_postal_codes=["77505", "01000", "77500"])

    async def change() -> DeliverySettings:
        await repo.save(settings)
        return await repo.get()

    assert await in_transaction(container, change) == settings
    assert await SqlDeliverySettingsReader(container.database).current() == settings
    async with container.database.engine.connect() as connection:
        stored = (
            await connection.execute(
                text("SELECT local_postal_codes FROM orders.delivery_settings")
            )
        ).scalar_one()
    assert stored == ["01000", "77500", "77505"]


async def test_a_null_free_from_survives_a_round_trip(container: Container) -> None:
    repo = SqlDeliverySettingsRepository(container.database)

    async def change() -> None:
        await repo.save(build_settings(home_free_from_cents=250000))
        await repo.save(build_settings(home_free_from_cents=None))

    await in_transaction(container, change)

    current = await SqlDeliverySettingsReader(container.database).current()
    assert current.home.free_from is None
    async with container.database.engine.connect() as connection:
        stored = (
            await connection.execute(
                text("SELECT home_free_from_cents IS NULL FROM orders.delivery_settings")
            )
        ).scalar_one()
    assert stored is True


async def test_an_empty_postal_code_list_and_blank_pickup_round_trip(container: Container) -> None:
    repo = SqlDeliverySettingsRepository(container.database)
    off = build_settings(
        local_enabled=False,
        local_postal_codes=[],
        pickup_enabled=False,
        pickup_address="",
        pickup_hours="",
    )

    await in_transaction(container, lambda: repo.save(off))

    assert await SqlDeliverySettingsReader(container.database).current() == off


async def test_saving_twice_upserts_the_single_row(container: Container) -> None:
    repo = SqlDeliverySettingsRepository(container.database)
    first = build_settings()
    second = build_settings(home_price_cents=100, local_postal_codes=["11111"])

    await in_transaction(container, lambda: repo.save(first))
    await in_transaction(container, lambda: repo.save(second))

    assert await SqlDeliverySettingsReader(container.database).current() == second
    assert await row_count(container) == 1


async def test_the_settings_are_stored_under_row_id_one(container: Container) -> None:
    repo = SqlDeliverySettingsRepository(container.database)

    await in_transaction(container, lambda: repo.save(build_settings()))

    async with container.database.engine.connect() as connection:
        ids = (
            (await connection.execute(text("SELECT id FROM orders.delivery_settings")))
            .scalars()
            .all()
        )
    assert ids == [1]


async def test_a_rolled_back_transaction_does_not_change_the_settings(
    container: Container,
) -> None:
    repo = SqlDeliverySettingsRepository(container.database)

    async def failing() -> Result[None, DomainError]:
        await repo.save(build_settings())
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await SqlTransactionRunner(container.database).run(failing)

    assert await SqlDeliverySettingsReader(container.database).current() == (
        DeliverySettings.default()
    )


async def test_the_reader_sees_the_settings_without_an_active_transaction(
    container: Container,
) -> None:
    repo = SqlDeliverySettingsRepository(container.database)
    await in_transaction(container, lambda: repo.save(build_settings()))

    current = await SqlDeliverySettingsReader(container.database).current()

    assert current.local.postal_codes == frozenset({PostalCode("77500"), PostalCode("77505")})


# --- table constraints -------------------------------------------------------------------------

_INSERT = (
    "INSERT INTO orders.delivery_settings (id, home_enabled, home_price_cents, "
    "home_free_from_cents, local_enabled, local_price_cents, local_postal_codes, "
    "pickup_enabled, pickup_address, pickup_hours) "
    "VALUES (:id, false, 0, NULL, false, 0, {codes}, false, {address}, '')"
)


async def test_the_table_holds_only_one_row_per_id(container: Container) -> None:
    with pytest.raises(IntegrityError):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text(_INSERT.format(codes="ARRAY[]::varchar(5)[]", address="''")), {"id": 1}
            )


@pytest.mark.parametrize(
    "column",
    [
        "home_enabled",
        "home_price_cents",
        "local_enabled",
        "local_price_cents",
        "local_postal_codes",
        "pickup_enabled",
        "pickup_address",
        "pickup_hours",
    ],
)
async def test_every_column_but_free_from_rejects_null(container: Container, column: str) -> None:
    with pytest.raises(IntegrityError):
        async with container.database.engine.begin() as connection:
            await connection.execute(update(delivery_settings).values({column: None}))


async def test_the_database_bounds_a_postal_code_to_five_characters(container: Container) -> None:
    with pytest.raises(DataError):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE orders.delivery_settings SET local_postal_codes = ARRAY['123456']")
            )


async def test_the_database_bounds_the_pickup_address_to_300_and_hours_to_200(
    container: Container,
) -> None:
    for column, size in (("pickup_address", 300), ("pickup_hours", 200)):
        with pytest.raises(DataError):
            async with container.database.engine.begin() as connection:
                await connection.execute(
                    update(delivery_settings).values({column: "a" * (size + 1)})
                )
        async with container.database.engine.begin() as connection:
            await connection.execute(update(delivery_settings).values({column: "a" * size}))


# --- the row lock ------------------------------------------------------------------------------


async def test_get_for_update_blocks_a_second_writer_until_the_first_commits(
    container: Container,
) -> None:
    repo = SqlDeliverySettingsRepository(container.database)
    runner = SqlTransactionRunner(container.database)
    holding = asyncio.Event()
    release = asyncio.Event()
    second_done = asyncio.Event()

    async def first() -> Result[None, DomainError]:
        await repo.get_for_update()
        holding.set()
        await asyncio.wait_for(release.wait(), TIMEOUT)
        await repo.save(build_settings(home_price_cents=111))
        return Ok(None)

    async def second() -> Result[None, DomainError]:
        current = await repo.get_for_update()
        # It only gets the row after the first committed, so it sees the first one's price.
        assert current.home.price.money.cents == 111
        await repo.save(build_settings(home_price_cents=222))
        second_done.set()
        return Ok(None)

    first_task = asyncio.create_task(runner.run(first))
    second_task: asyncio.Task[Result[None, DomainError]] | None = None
    try:
        await asyncio.wait_for(holding.wait(), TIMEOUT)
        second_task = asyncio.create_task(runner.run(second))
        await asyncio.sleep(WAIT)
        assert not second_done.is_set(), "the second writer got the row while it was locked"
        release.set()
        assert isinstance(await asyncio.wait_for(first_task, TIMEOUT), Ok)
        assert isinstance(await asyncio.wait_for(second_task, TIMEOUT), Ok)
    finally:
        release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()

    final = await SqlDeliverySettingsReader(container.database).current()
    assert final.home.price.money.cents == 222


# --- use cases over the real adapters ----------------------------------------------------------


async def test_the_default_database_state_offers_nothing_publicly(container: Container) -> None:
    public = await container.services.get(GetPublicDeliveryOptions).execute()
    quoted = await container.services.get(QuoteDelivery).execute("77500", 300000)

    assert (public.home, public.local, public.pickup) == (None, None, None)
    assert isinstance(quoted, Ok)
    assert quoted.value.options == []


async def test_update_then_read_back_and_quote_through_the_container(
    container: Container,
) -> None:
    result = await container.services.get(UpdateDeliveryOptions).execute(full_options())
    assert result == Ok(None)

    admin = await container.services.get(GetDeliveryOptions).execute()
    public = await container.services.get(GetPublicDeliveryOptions).execute()
    quoted = await container.services.get(QuoteDelivery).execute("77500", 250000)

    assert admin.local.postal_codes == ["01000", "77500", "77505"]
    assert admin.home == full_options().home
    assert admin.pickup == full_options().pickup
    assert public.home is not None
    assert public.home.free_from_cents == 250000
    assert isinstance(quoted, Ok)
    assert [(line.method, line.cost_cents) for line in quoted.value.options] == [
        ("home", 0),
        ("local", 9000),
        ("pickup", 0),
    ]


async def test_an_invalid_update_leaves_the_stored_configuration_untouched(
    container: Container,
) -> None:
    update = container.services.get(UpdateDeliveryOptions)
    await update.execute(full_options())
    broken = full_options().model_copy(
        update={"local": LocalDeliveryOptions(enabled=True, price_cents=1, postal_codes=[])}
    )

    result = await update.execute(broken)

    assert isinstance(result, Err)
    assert await container.services.get(GetDeliveryOptions).execute() == (
        full_options().model_copy(
            update={
                "local": LocalDeliveryOptions(
                    enabled=True, price_cents=9000, postal_codes=["01000", "77500", "77505"]
                )
            }
        )
    )
    assert await row_count(container) == 1


async def test_switching_home_off_keeps_the_price_in_the_database(container: Container) -> None:
    update = container.services.get(UpdateDeliveryOptions)
    off = full_options().model_copy(
        update={"home": HomeDeliveryOptions(enabled=False, price_cents=18000, free_from_cents=None)}
    )

    await update.execute(off)

    assert (await container.services.get(GetPublicDeliveryOptions).execute()).home is None
    assert (await container.services.get(GetDeliveryOptions).execute()).home == off.home
    quoted = await container.services.get(QuoteDelivery).execute("77500", 1)
    assert isinstance(quoted, Ok)
    assert [line.method for line in quoted.value.options] == ["local", "pickup"]


async def test_leaves_only_the_seeded_default_row_between_tests(container: Container) -> None:
    """The autouse fixture's promise: one row, the default one, whatever ran before."""
    assert await row_count(container) == 1
    assert await SqlDeliverySettingsReader(container.database).current() == (
        DeliverySettings.default()
    )
