"""Domain of the delivery options: value rules, `default()`, `create()` and `quote`."""

import pytest

from fragancia_api.modules.orders.domain.delivery import (
    DELIVERY_PRICE_MAX_CENTS,
    FREE_FROM_MAX_CENTS,
    PICKUP_ADDRESS_MAX,
    PICKUP_HOURS_MAX,
    POSTAL_CODES_MAX,
    QUOTE_SUBTOTAL_MAX_CENTS,
    DeliveryMethod,
    DeliveryPrice,
    DeliveryQuote,
    DeliverySettings,
    PostalCode,
    Subtotal,
    quote,
)
from fragancia_api.modules.orders.domain.errors import DeliveryOptionsInvalid, PostalCodeInvalid
from fragancia_api.shared.kernel import Err, Money, Ok
from tests.unit.orders.support import build_settings

CREATE_VALID = {
    "home_enabled": True,
    "home_price_cents": 18000,
    "home_free_from_cents": 250000,
    "local_enabled": True,
    "local_price_cents": 9000,
    "local_postal_codes": ["77500", "77505"],
    "pickup_enabled": True,
    "pickup_address": "Calle 1",
    "pickup_hours": "Lun-Vie",
}


def invalid_field(**overrides: object) -> dict[str, object]:
    """The `details` of the error `create` returns for these fields."""
    values = {**CREATE_VALID, **overrides}
    result = DeliverySettings.create(**values)  # type: ignore[arg-type]
    assert isinstance(result, Err), "expected the settings to be rejected"
    assert isinstance(result.error, DeliveryOptionsInvalid)
    assert result.error.code == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert result.error.details is not None
    return dict(result.error.details)


def accepted(**overrides: object) -> DeliverySettings:
    return build_settings(**overrides)


# --- PostalCode --------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["77500", "01000", "00000", "  77500  ", "\n77500\t"])
def test_a_postal_code_is_five_digits_after_stripping(raw: str) -> None:
    result = PostalCode.create(raw)

    assert result == Ok(PostalCode(raw.strip()))


@pytest.mark.parametrize(
    "raw", ["", "    ", "7750", "775000", "abcde", "7750a", "77 00", "-7750", "7750.", "٧٧٥٠٠"]
)
def test_rejects_a_postal_code_that_is_not_five_ascii_digits(raw: str) -> None:
    result = PostalCode.create(raw)

    assert isinstance(result, Err)
    assert isinstance(result.error, PostalCodeInvalid)
    assert result.error.code == "ORDERS_POSTAL_CODE_INVALID"


def test_a_postal_code_keeps_its_leading_zero() -> None:
    assert PostalCode.create("01000") == Ok(PostalCode("01000"))
    assert PostalCode("01000") != PostalCode("1000")


# --- Subtotal ----------------------------------------------------------------------------------


@pytest.mark.parametrize("cents", [0, 1, QUOTE_SUBTOTAL_MAX_CENTS])
def test_a_subtotal_accepts_zero_up_to_the_sanity_bound(cents: int) -> None:
    assert Subtotal.create(cents) == Ok(Subtotal(Money(cents)))


@pytest.mark.parametrize("cents", [-1, QUOTE_SUBTOTAL_MAX_CENTS + 1])
def test_rejects_a_subtotal_outside_the_bounds_naming_the_field(cents: int) -> None:
    result = Subtotal.create(cents)

    assert isinstance(result, Err)
    assert isinstance(result.error, DeliveryOptionsInvalid)
    assert result.error.code == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert result.error.details == {"field": "subtotal_cents", "min": 0, "max": 100_000_000}


def test_the_subtotal_bound_is_the_plan_value() -> None:
    assert QUOTE_SUBTOTAL_MAX_CENTS == 100_000_000


# --- DeliveryPrice -----------------------------------------------------------------------------


@pytest.mark.parametrize("cents", [0, 1, DELIVERY_PRICE_MAX_CENTS])
def test_a_delivery_price_accepts_zero_up_to_the_sanity_bound(cents: int) -> None:
    assert DeliveryPrice.create(cents, field="x.price") == Ok(DeliveryPrice(Money(cents)))


@pytest.mark.parametrize("cents", [-1, DELIVERY_PRICE_MAX_CENTS + 1])
def test_rejects_a_delivery_price_outside_the_bounds_naming_the_field(cents: int) -> None:
    result = DeliveryPrice.create(cents, field="x.price")

    assert isinstance(result, Err)
    assert result.error.details == {"field": "x.price", "min": 0, "max": DELIVERY_PRICE_MAX_CENTS}


def test_the_documented_bounds_are_the_plan_values() -> None:
    assert DELIVERY_PRICE_MAX_CENTS == 1_000_000
    assert FREE_FROM_MAX_CENTS == 10_000_000
    assert POSTAL_CODES_MAX == 500
    assert (PICKUP_ADDRESS_MAX, PICKUP_HOURS_MAX) == (300, 200)


# --- default -----------------------------------------------------------------------------------


def test_the_default_has_every_method_off_and_empty() -> None:
    settings = DeliverySettings.default()

    assert settings.home.enabled is False
    assert settings.home.price.money == Money(0)
    assert settings.home.free_from is None
    assert settings.local.enabled is False
    assert settings.local.price.money == Money(0)
    assert settings.local.postal_codes == frozenset()
    assert settings.pickup.enabled is False
    assert (settings.pickup.address, settings.pickup.hours) == ("", "")


def test_the_default_quotes_nothing_for_any_input() -> None:
    settings = DeliverySettings.default()

    assert quote(settings, PostalCode("77500"), Subtotal(Money(1_000_000))) == []
    assert quote(settings, None, Subtotal(Money(0))) == []


# --- create: valid -----------------------------------------------------------------------------


def test_creates_settings_from_valid_fields() -> None:
    settings = accepted()

    assert settings.home.enabled is True
    assert settings.home.price.money == Money(18000)
    assert settings.home.free_from == Money(250000)
    assert settings.local.price.money == Money(9000)
    assert settings.local.postal_codes == frozenset({PostalCode("77500"), PostalCode("77505")})
    assert settings.pickup.address == "Calle 1 #2, Cancun"
    assert settings.pickup.hours == "Lun-Vie 10-18"


def test_home_free_from_is_optional() -> None:
    assert accepted(home_free_from_cents=None).home.free_from is None


@pytest.mark.parametrize("cents", [1, FREE_FROM_MAX_CENTS])
def test_free_from_accepts_its_bounds(cents: int) -> None:
    assert accepted(home_free_from_cents=cents).home.free_from == Money(cents)


@pytest.mark.parametrize("cents", [0, -1, FREE_FROM_MAX_CENTS + 1])
def test_rejects_free_from_outside_its_bounds(cents: int) -> None:
    assert invalid_field(home_free_from_cents=cents) == {
        "field": "home.free_from_cents",
        "min": 1,
        "max": FREE_FROM_MAX_CENTS,
    }


def test_prices_are_validated_even_while_the_method_is_off() -> None:
    assert invalid_field(home_enabled=False, home_price_cents=-1)["field"] == "home.price_cents"
    assert invalid_field(local_enabled=False, local_price_cents=-1)["field"] == "local.price_cents"


def test_an_off_method_keeps_its_configured_values() -> None:
    settings = accepted(home_enabled=False)

    assert settings.home.enabled is False
    assert settings.home.price.money == Money(18000)
    assert settings.home.free_from == Money(250000)


@pytest.mark.parametrize(
    ("argument", "field"),
    [("home_price_cents", "home.price_cents"), ("local_price_cents", "local.price_cents")],
)
@pytest.mark.parametrize("value", [-1, DELIVERY_PRICE_MAX_CENTS + 1])
def test_rejects_a_price_outside_the_bounds(argument: str, field: str, value: int) -> None:
    assert invalid_field(**{argument: value})["field"] == field


# --- create: local postal codes ----------------------------------------------------------------


def test_local_delivery_on_needs_at_least_one_postal_code() -> None:
    assert invalid_field(local_postal_codes=[]) == {"field": "local.postal_codes", "min": 1}


def test_local_delivery_off_may_have_no_postal_codes() -> None:
    settings = accepted(local_enabled=False, local_postal_codes=[])

    assert settings.local.postal_codes == frozenset()


@pytest.mark.parametrize("bad", ["7750", "abcde", "", "775000"])
def test_rejects_a_malformed_postal_code_in_the_list(bad: str) -> None:
    assert invalid_field(local_postal_codes=["77500", bad])["field"] == "local.postal_codes"


def test_an_invalid_postal_code_is_named_by_index_and_value() -> None:
    details = invalid_field(local_postal_codes=["77500", "7750", "77505"])

    assert details == {"field": "local.postal_codes", "index": 1, "value": "7750"}


def test_the_first_invalid_postal_code_is_the_one_reported() -> None:
    details = invalid_field(local_postal_codes=["abcde", "77500", "7750"])

    assert details["index"] == 0
    assert details["value"] == "abcde"


def test_the_postal_code_index_is_the_position_in_the_request() -> None:
    details = invalid_field(local_postal_codes=[" 77500", "77500 ", "bad"])

    assert details["index"] == 2


def test_the_reported_postal_code_value_is_truncated_to_twenty_characters() -> None:
    details = invalid_field(local_postal_codes=["x" * 25])

    assert details["value"] == "x" * 20
    assert details["index"] == 0


def test_a_postal_code_value_of_exactly_twenty_characters_is_kept_whole() -> None:
    assert invalid_field(local_postal_codes=["y" * 20])["value"] == "y" * 20


def test_postal_codes_are_stripped_and_deduplicated() -> None:
    settings = accepted(local_postal_codes=[" 77500", "77500 ", "77505"])

    assert settings.local.postal_codes == frozenset({PostalCode("77500"), PostalCode("77505")})


def test_accepts_exactly_the_maximum_number_of_distinct_postal_codes() -> None:
    codes = [f"{n:05d}" for n in range(POSTAL_CODES_MAX)]

    assert len(accepted(local_postal_codes=codes).local.postal_codes) == POSTAL_CODES_MAX


def test_rejects_more_distinct_postal_codes_than_the_maximum() -> None:
    codes = [f"{n:05d}" for n in range(POSTAL_CODES_MAX + 1)]

    assert invalid_field(local_postal_codes=codes) == {
        "field": "local.postal_codes",
        "max": POSTAL_CODES_MAX,
    }


def test_duplicates_do_not_count_toward_the_maximum() -> None:
    codes = [f"{n:05d}" for n in range(POSTAL_CODES_MAX)] + ["00000", "00001"]

    assert len(accepted(local_postal_codes=codes).local.postal_codes) == POSTAL_CODES_MAX


# --- create: pickup ----------------------------------------------------------------------------


@pytest.mark.parametrize("address", ["", "   ", "\n\t"])
def test_pickup_on_needs_an_address(address: str) -> None:
    assert invalid_field(pickup_address=address) == {
        "field": "pickup.address",
        "min": 1,
        "max": PICKUP_ADDRESS_MAX,
    }


@pytest.mark.parametrize("hours", ["", "   "])
def test_pickup_on_needs_hours(hours: str) -> None:
    assert invalid_field(pickup_hours=hours) == {
        "field": "pickup.hours",
        "min": 1,
        "max": PICKUP_HOURS_MAX,
    }


def test_pickup_off_may_have_empty_address_and_hours() -> None:
    settings = accepted(pickup_enabled=False, pickup_address="", pickup_hours="  ")

    assert (settings.pickup.address, settings.pickup.hours) == ("", "")


def test_pickup_strings_are_stripped_and_inner_whitespace_is_kept() -> None:
    settings = accepted(pickup_address="  Calle 1   Piso 2 \n", pickup_hours="\tLun   a Vie ")

    assert settings.pickup.address == "Calle 1   Piso 2"
    assert settings.pickup.hours == "Lun   a Vie"


def test_pickup_address_accepts_its_maximum_length_and_rejects_one_more() -> None:
    assert len(accepted(pickup_address="a" * PICKUP_ADDRESS_MAX).pickup.address) == 300
    assert invalid_field(pickup_address="a" * (PICKUP_ADDRESS_MAX + 1))["field"] == (
        "pickup.address"
    )


def test_pickup_hours_accepts_its_maximum_length_and_rejects_one_more() -> None:
    assert len(accepted(pickup_hours="h" * PICKUP_HOURS_MAX).pickup.hours) == 200
    assert invalid_field(pickup_hours="h" * (PICKUP_HOURS_MAX + 1))["field"] == "pickup.hours"


def test_the_pickup_maximums_apply_while_the_method_is_off() -> None:
    too_long = "a" * (PICKUP_ADDRESS_MAX + 1)

    assert invalid_field(pickup_enabled=False, pickup_address=too_long)["field"] == (
        "pickup.address"
    )


def test_padding_does_not_count_toward_the_pickup_maximum() -> None:
    padded = "  " + "a" * PICKUP_ADDRESS_MAX + "  "

    assert len(accepted(pickup_address=padded).pickup.address) == PICKUP_ADDRESS_MAX


# --- create: which error comes first -----------------------------------------------------------


def test_the_first_broken_rule_is_reported_in_the_order_home_local_pickup() -> None:
    broken = {
        "home_price_cents": -1,
        "local_postal_codes": [],
        "pickup_address": "",
    }

    assert invalid_field(**broken)["field"] == "home.price_cents"
    assert invalid_field(**{**broken, "home_price_cents": 0})["field"] == "local.postal_codes"
    assert (
        invalid_field(**{**broken, "home_price_cents": 0, "local_postal_codes": ["77500"]})["field"]
        == "pickup.address"
    )


# --- quote -------------------------------------------------------------------------------------

CODE = PostalCode("77500")


def costs(lines: list[DeliveryQuote]) -> dict[DeliveryMethod, int]:
    return {line.method: line.cost.cents for line in lines}


def test_quotes_every_method_in_the_order_home_local_pickup() -> None:
    lines = quote(build_settings(), CODE, Subtotal(Money(100000)))

    assert [line.method for line in lines] == [
        DeliveryMethod.HOME,
        DeliveryMethod.LOCAL,
        DeliveryMethod.PICKUP,
    ]
    assert costs(lines) == {
        DeliveryMethod.HOME: 18000,
        DeliveryMethod.LOCAL: 9000,
        DeliveryMethod.PICKUP: 0,
    }


def test_home_is_free_when_the_subtotal_equals_free_from() -> None:
    lines = quote(build_settings(), CODE, Subtotal(Money(250000)))

    assert costs(lines)[DeliveryMethod.HOME] == 0


def test_quote_prices_against_a_validated_subtotal() -> None:
    subtotal = Subtotal.create(250000)
    assert isinstance(subtotal, Ok)

    lines = quote(build_settings(), CODE, subtotal.value)

    assert costs(lines)[DeliveryMethod.HOME] == 0


def test_home_is_charged_one_cent_below_free_from() -> None:
    lines = quote(build_settings(), CODE, Subtotal(Money(249999)))

    assert costs(lines)[DeliveryMethod.HOME] == 18000


def test_home_is_free_above_free_from() -> None:
    assert costs(quote(build_settings(), CODE, Subtotal(Money(250001))))[DeliveryMethod.HOME] == 0


def test_home_is_never_free_when_free_from_is_unset() -> None:
    settings = build_settings(home_free_from_cents=None)

    assert costs(quote(settings, CODE, Subtotal(Money(100_000_000))))[DeliveryMethod.HOME] == 18000


def test_home_charges_its_price_for_an_empty_cart() -> None:
    assert costs(quote(build_settings(), CODE, Subtotal(Money(0))))[DeliveryMethod.HOME] == 18000


def test_free_from_does_not_make_local_delivery_free() -> None:
    lines = quote(build_settings(), CODE, Subtotal(Money(250000)))

    assert costs(lines)[DeliveryMethod.LOCAL] == 9000


def test_home_at_price_zero_costs_zero_below_free_from() -> None:
    settings = build_settings(home_price_cents=0)

    assert costs(quote(settings, None, Subtotal(Money(1))))[DeliveryMethod.HOME] == 0


def test_local_is_absent_for_a_postal_code_off_the_list() -> None:
    lines = quote(build_settings(), PostalCode("01000"), Subtotal(Money(100000)))

    assert [line.method for line in lines] == [DeliveryMethod.HOME, DeliveryMethod.PICKUP]


def test_local_applies_to_every_code_on_the_list() -> None:
    lines = quote(build_settings(), PostalCode("77505"), Subtotal(Money(100000)))

    assert DeliveryMethod.LOCAL in costs(lines)


def test_without_a_postal_code_only_home_and_pickup_apply() -> None:
    lines = quote(build_settings(), None, Subtotal(Money(100000)))

    assert [line.method for line in lines] == [DeliveryMethod.HOME, DeliveryMethod.PICKUP]


def test_a_method_that_is_off_is_not_quoted() -> None:
    lines = quote(build_settings(home_enabled=False), CODE, Subtotal(Money(100000)))

    assert [line.method for line in lines] == [DeliveryMethod.LOCAL, DeliveryMethod.PICKUP]


def test_local_off_is_not_quoted_even_for_a_listed_code() -> None:
    lines = quote(build_settings(local_enabled=False), CODE, Subtotal(Money(100000)))

    assert [line.method for line in lines] == [DeliveryMethod.HOME, DeliveryMethod.PICKUP]


def test_pickup_off_is_not_quoted() -> None:
    lines = quote(build_settings(pickup_enabled=False), CODE, Subtotal(Money(100000)))

    assert [line.method for line in lines] == [DeliveryMethod.HOME, DeliveryMethod.LOCAL]


def test_pickup_is_always_free() -> None:
    assert costs(quote(build_settings(), None, Subtotal(Money(0))))[DeliveryMethod.PICKUP] == 0


def test_nothing_applies_when_every_method_is_off() -> None:
    settings = build_settings(home_enabled=False, local_enabled=False, pickup_enabled=False)

    assert quote(settings, CODE, Subtotal(Money(100000))) == []


def test_switching_home_off_keeps_the_stored_price_for_when_it_is_switched_on() -> None:
    off = build_settings(home_enabled=False)

    assert off.home.price.money == Money(18000)
    assert DeliveryMethod.HOME not in costs(quote(off, CODE, Subtotal(Money(1))))
