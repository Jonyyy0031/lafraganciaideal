"""Use cases of the delivery options over the in-memory adapter."""

from collections.abc import Awaitable, Callable

import pytest

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
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryDeliverySettings
from fragancia_api.shared.infrastructure.in_memory import InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, Result
from tests.unit.orders.support import build_settings


def options(
    *,
    home: HomeDeliveryOptions | None = None,
    local: LocalDeliveryOptions | None = None,
    pickup: PickupOptions | None = None,
) -> DeliveryOptions:
    return DeliveryOptions(
        home=home or HomeDeliveryOptions(enabled=True, price_cents=18000, free_from_cents=250000),
        local=local
        or LocalDeliveryOptions(enabled=True, price_cents=9000, postal_codes=["77505", "77500"]),
        pickup=pickup or PickupOptions(enabled=True, address="Calle 1", hours="Lun-Vie"),
    )


class Calls(list[str]):
    """What the command did, in order, including where the transaction began and ended."""


class LoggedSettings(InMemoryDeliverySettings):
    def __init__(self, calls: Calls) -> None:
        super().__init__()
        self._calls = calls

    async def get(self) -> DeliverySettings:
        self._calls.append("get")
        return await super().get()

    async def get_for_update(self) -> DeliverySettings:
        self._calls.append("get_for_update")
        return await super().get_for_update()

    async def save(self, settings: DeliverySettings) -> None:
        self._calls.append("save")
        await super().save(settings)


class LoggedRunner(InMemoryTransactionRunner):
    def __init__(self, calls: Calls) -> None:
        self._calls = calls

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        self._calls.append("begin")
        try:
            return await work()
        finally:
            self._calls.append("end")


# --- UpdateDeliveryOptions ---------------------------------------------------------------------


async def test_updating_stores_the_whole_configuration() -> None:
    store = InMemoryDeliverySettings()
    command = UpdateDeliveryOptions(repository=store, transactions=InMemoryTransactionRunner())

    result = await command.execute(options())

    assert result == Ok(None)
    assert store.settings == build_settings(
        pickup_address="Calle 1", pickup_hours="Lun-Vie", local_postal_codes=["77500", "77505"]
    )


async def test_updating_locks_the_row_then_saves_inside_the_transaction() -> None:
    calls = Calls()
    command = UpdateDeliveryOptions(
        repository=LoggedSettings(calls), transactions=LoggedRunner(calls)
    )

    await command.execute(options())

    assert calls == ["begin", "get_for_update", "save", "end"]


async def test_updating_replaces_the_previous_configuration_not_merges_it() -> None:
    store = InMemoryDeliverySettings(build_settings())
    command = UpdateDeliveryOptions(repository=store, transactions=InMemoryTransactionRunner())

    await command.execute(
        options(
            home=HomeDeliveryOptions(enabled=False, price_cents=500, free_from_cents=None),
            local=LocalDeliveryOptions(enabled=False, price_cents=0, postal_codes=[]),
            pickup=PickupOptions(enabled=False, address="", hours=""),
        )
    )

    assert store.settings.home.enabled is False
    assert store.settings.home.free_from is None
    assert store.settings.local.postal_codes == frozenset()
    assert store.settings.pickup.address == ""


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            options(local=LocalDeliveryOptions(enabled=True, price_cents=9000, postal_codes=[])),
            "local.postal_codes",
        ),
        (
            options(pickup=PickupOptions(enabled=True, address="", hours="Lun")),
            "pickup.address",
        ),
        (
            options(pickup=PickupOptions(enabled=True, address="Calle", hours="")),
            "pickup.hours",
        ),
        (
            options(home=HomeDeliveryOptions(enabled=True, price_cents=-1, free_from_cents=None)),
            "home.price_cents",
        ),
        (
            options(home=HomeDeliveryOptions(enabled=True, price_cents=0, free_from_cents=0)),
            "home.free_from_cents",
        ),
        (
            options(local=LocalDeliveryOptions(enabled=True, price_cents=1, postal_codes=["7750"])),
            "local.postal_codes",
        ),
    ],
)
async def test_updating_rejects_invalid_options_without_touching_the_store(
    body: DeliveryOptions, field: str
) -> None:
    calls = Calls()
    store = LoggedSettings(calls)
    command = UpdateDeliveryOptions(repository=store, transactions=LoggedRunner(calls))

    result = await command.execute(body)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert result.error.details is not None
    assert result.error.details["field"] == field
    assert calls == []
    assert store.settings == DeliverySettings.default()


# --- GetDeliveryOptions ------------------------------------------------------------------------


async def test_the_admin_query_returns_the_default_with_everything_off() -> None:
    result = await GetDeliveryOptions(InMemoryDeliverySettings()).execute()

    assert result == DeliveryOptions(
        home=HomeDeliveryOptions(enabled=False, price_cents=0, free_from_cents=None),
        local=LocalDeliveryOptions(enabled=False, price_cents=0, postal_codes=[]),
        pickup=PickupOptions(enabled=False, address="", hours=""),
    )


async def test_the_admin_query_returns_what_was_stored_with_sorted_postal_codes() -> None:
    store = InMemoryDeliverySettings(build_settings(local_postal_codes=["77505", "01000", "77500"]))

    result = await GetDeliveryOptions(store).execute()

    assert result.home == HomeDeliveryOptions(
        enabled=True, price_cents=18000, free_from_cents=250000
    )
    assert result.local.postal_codes == ["01000", "77500", "77505"]
    assert result.pickup == PickupOptions(
        enabled=True, address="Calle 1 #2, Cancun", hours="Lun-Vie 10-18"
    )


async def test_the_admin_query_still_shows_a_method_that_is_off_with_its_price() -> None:
    store = InMemoryDeliverySettings(build_settings(home_enabled=False))

    result = await GetDeliveryOptions(store).execute()

    assert result.home == HomeDeliveryOptions(
        enabled=False, price_cents=18000, free_from_cents=250000
    )


# --- GetPublicDeliveryOptions ------------------------------------------------------------------


async def test_the_public_query_shows_no_method_by_default() -> None:
    result = await GetPublicDeliveryOptions(InMemoryDeliverySettings()).execute()

    assert (result.home, result.local, result.pickup) == (None, None, None)


async def test_the_public_query_lists_the_enabled_methods() -> None:
    store = InMemoryDeliverySettings(build_settings(local_postal_codes=["77505", "77500"]))

    result = await GetPublicDeliveryOptions(store).execute()

    assert result.home is not None
    assert (result.home.price_cents, result.home.free_from_cents) == (18000, 250000)
    assert result.local is not None
    assert (result.local.price_cents, result.local.postal_codes) == (9000, ["77500", "77505"])
    assert result.pickup is not None
    assert (result.pickup.address, result.pickup.hours) == ("Calle 1 #2, Cancun", "Lun-Vie 10-18")


@pytest.mark.parametrize("method", ["home", "local", "pickup"])
async def test_the_public_query_hides_only_the_disabled_method(method: str) -> None:
    store = InMemoryDeliverySettings(build_settings(**{f"{method}_enabled": False}))

    result = await GetPublicDeliveryOptions(store).execute()

    shown = {"home": result.home, "local": result.local, "pickup": result.pickup}
    assert shown.pop(method) is None
    assert all(value is not None for value in shown.values())


async def test_the_public_query_without_free_from_reports_none() -> None:
    store = InMemoryDeliverySettings(build_settings(home_free_from_cents=None))

    result = await GetPublicDeliveryOptions(store).execute()

    assert result.home is not None
    assert result.home.free_from_cents is None


# --- QuoteDelivery -----------------------------------------------------------------------------


def quote_query(settings: DeliverySettings | None = None) -> QuoteDelivery:
    return QuoteDelivery(InMemoryDeliverySettings(settings or build_settings()))


async def lines(
    query: QuoteDelivery, postal_code: str | None, subtotal: int
) -> list[tuple[str, int]]:
    result = await query.execute(postal_code, subtotal)
    assert isinstance(result, Ok), result
    return [(line.method, line.cost_cents) for line in result.value.options]


async def test_quotes_the_methods_that_apply() -> None:
    assert await lines(quote_query(), "77500", 100000) == [
        ("home", 18000),
        ("local", 9000),
        ("pickup", 0),
    ]


async def test_quotes_home_as_free_at_the_free_from_subtotal() -> None:
    assert (await lines(quote_query(), "77500", 250000))[0] == ("home", 0)


async def test_quotes_without_local_for_a_postal_code_off_the_list() -> None:
    assert await lines(quote_query(), "01000", 100000) == [("home", 18000), ("pickup", 0)]


async def test_quotes_home_and_pickup_without_a_postal_code() -> None:
    assert await lines(quote_query(), None, 100000) == [("home", 18000), ("pickup", 0)]


async def test_a_postal_code_is_stripped_before_matching() -> None:
    assert ("local", 9000) in await lines(quote_query(), " 77500 ", 100000)


async def test_quotes_nothing_for_the_default_settings() -> None:
    assert await lines(quote_query(DeliverySettings.default()), "77500", 100000) == []


@pytest.mark.parametrize("postal_code", ["abc", "7750", "775000", "", "7750a"])
async def test_rejects_a_malformed_postal_code(postal_code: str) -> None:
    result = await quote_query().execute(postal_code, 100000)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_POSTAL_CODE_INVALID"


@pytest.mark.parametrize("subtotal", [-1, 100_000_001])
async def test_rejects_a_subtotal_outside_the_bounds_naming_the_field(subtotal: int) -> None:
    result = await quote_query().execute("77500", subtotal)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert result.error.details == {"field": "subtotal_cents", "min": 0, "max": 100_000_000}


@pytest.mark.parametrize("subtotal", [0, 100_000_000])
async def test_accepts_the_subtotal_bounds(subtotal: int) -> None:
    result = await quote_query().execute("77500", subtotal)

    assert isinstance(result, Ok)


async def test_a_bad_postal_code_is_reported_before_a_bad_subtotal() -> None:
    result = await quote_query().execute("abc", -1)

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_POSTAL_CODE_INVALID"
