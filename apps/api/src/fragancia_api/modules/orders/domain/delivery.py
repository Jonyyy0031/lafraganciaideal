"""Delivery options: three methods (home, local, pickup), each with an on/off switch, and the
pure `quote` that prices them for a postal code and a subtotal."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from fragancia_api.modules.orders.domain.errors import DeliveryOptionsInvalid, PostalCodeInvalid
from fragancia_api.shared.kernel import Err, Money, Ok, Result

# A sanity bound ($10,000), not a business price.
DELIVERY_PRICE_MAX_CENTS = 1_000_000
FREE_FROM_MAX_CENTS = 10_000_000
POSTAL_CODES_MAX = 500
PICKUP_ADDRESS_MAX = 300
PICKUP_HOURS_MAX = 200
QUOTE_SUBTOTAL_MAX_CENTS = 100_000_000


class DeliveryMethod(StrEnum):
    HOME = "home"
    LOCAL = "local"
    PICKUP = "pickup"


@dataclass(frozen=True, slots=True)
class PostalCode:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, PostalCodeInvalid]:
        value = raw.strip()
        if len(value) != 5 or not (value.isascii() and value.isdigit()):
            return Err(PostalCodeInvalid())
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class DeliveryPrice:
    money: Money

    @classmethod
    def create(cls, cents: int, *, field: str) -> Result[Self, DeliveryOptionsInvalid]:
        if not 0 <= cents <= DELIVERY_PRICE_MAX_CENTS:
            return Err(
                DeliveryOptionsInvalid(
                    details={"field": field, "min": 0, "max": DELIVERY_PRICE_MAX_CENTS}
                )
            )
        return Ok(cls(Money(cents)))


@dataclass(frozen=True, slots=True)
class HomeDelivery:
    enabled: bool
    price: DeliveryPrice
    free_from: Money | None


@dataclass(frozen=True, slots=True)
class LocalDelivery:
    enabled: bool
    price: DeliveryPrice
    postal_codes: frozenset[PostalCode]


@dataclass(frozen=True, slots=True)
class Pickup:
    enabled: bool
    address: str
    hours: str


def _home(
    enabled: bool, price_cents: int, free_from_cents: int | None
) -> Result[HomeDelivery, DeliveryOptionsInvalid]:
    match DeliveryPrice.create(price_cents, field="home.price_cents"):
        case Err(error):
            return Err(error)
        case Ok(price):
            pass
    free_from: Money | None = None
    if free_from_cents is not None:
        if not 1 <= free_from_cents <= FREE_FROM_MAX_CENTS:
            return Err(
                DeliveryOptionsInvalid(
                    details={
                        "field": "home.free_from_cents",
                        "min": 1,
                        "max": FREE_FROM_MAX_CENTS,
                    }
                )
            )
        free_from = Money(free_from_cents)
    return Ok(HomeDelivery(enabled, price, free_from))


def _local(
    enabled: bool, price_cents: int, postal_codes: list[str]
) -> Result[LocalDelivery, DeliveryOptionsInvalid]:
    match DeliveryPrice.create(price_cents, field="local.price_cents"):
        case Err(error):
            return Err(error)
        case Ok(price):
            pass
    codes: set[PostalCode] = set()
    for raw in postal_codes:
        match PostalCode.create(raw):
            case Err():
                return Err(
                    DeliveryOptionsInvalid(
                        message="A postal code is exactly 5 digits",
                        details={"field": "local.postal_codes"},
                    )
                )
            case Ok(code):
                codes.add(code)
    if len(codes) > POSTAL_CODES_MAX:
        return Err(
            DeliveryOptionsInvalid(details={"field": "local.postal_codes", "max": POSTAL_CODES_MAX})
        )
    if enabled and not codes:
        return Err(
            DeliveryOptionsInvalid(
                message="Local delivery needs at least one postal code",
                details={"field": "local.postal_codes", "min": 1},
            )
        )
    return Ok(LocalDelivery(enabled, price, frozenset(codes)))


def _pickup(enabled: bool, address: str, hours: str) -> Result[Pickup, DeliveryOptionsInvalid]:
    address = address.strip()
    hours = hours.strip()
    min_length = 1 if enabled else 0
    if not min_length <= len(address) <= PICKUP_ADDRESS_MAX:
        return Err(
            DeliveryOptionsInvalid(
                details={"field": "pickup.address", "min": min_length, "max": PICKUP_ADDRESS_MAX}
            )
        )
    if not min_length <= len(hours) <= PICKUP_HOURS_MAX:
        return Err(
            DeliveryOptionsInvalid(
                details={"field": "pickup.hours", "min": min_length, "max": PICKUP_HOURS_MAX}
            )
        )
    return Ok(Pickup(enabled, address, hours))


@dataclass(frozen=True, slots=True)
class DeliverySettings:
    home: HomeDelivery
    local: LocalDelivery
    pickup: Pickup

    @classmethod
    def default(cls) -> Self:
        """Every method off, prices 0, no postal codes, no pickup point."""
        zero = DeliveryPrice(Money(0))
        return cls(
            home=HomeDelivery(False, zero, None),
            local=LocalDelivery(False, zero, frozenset()),
            pickup=Pickup(False, "", ""),
        )

    @classmethod
    def create(
        cls,
        *,
        home_enabled: bool,
        home_price_cents: int,
        home_free_from_cents: int | None,
        local_enabled: bool,
        local_price_cents: int,
        local_postal_codes: list[str],
        pickup_enabled: bool,
        pickup_address: str,
        pickup_hours: str,
    ) -> Result[Self, DeliveryOptionsInvalid]:
        """Build every part; the first broken rule is the error."""
        match _home(home_enabled, home_price_cents, home_free_from_cents):
            case Err(error):
                return Err(error)
            case Ok(home):
                pass
        match _local(local_enabled, local_price_cents, local_postal_codes):
            case Err(error):
                return Err(error)
            case Ok(local):
                pass
        match _pickup(pickup_enabled, pickup_address, pickup_hours):
            case Err(error):
                return Err(error)
            case Ok(pickup):
                return Ok(cls(home, local, pickup))


@dataclass(frozen=True, slots=True)
class DeliveryQuote:
    method: DeliveryMethod
    cost: Money


def quote(
    settings: DeliverySettings, postal_code: PostalCode | None, subtotal: Money
) -> list[DeliveryQuote]:
    """The methods that apply, in the order home, local, pickup. Home is free when `free_from`
    is set and the subtotal reaches it; local needs a postal code on the list; pickup is
    always free. An empty list means nothing applies."""
    lines: list[DeliveryQuote] = []
    home = settings.home
    if home.enabled:
        free = home.free_from is not None and subtotal >= home.free_from
        lines.append(DeliveryQuote(DeliveryMethod.HOME, Money(0) if free else home.price.money))
    local = settings.local
    if local.enabled and postal_code is not None and postal_code in local.postal_codes:
        lines.append(DeliveryQuote(DeliveryMethod.LOCAL, local.price.money))
    if settings.pickup.enabled:
        lines.append(DeliveryQuote(DeliveryMethod.PICKUP, Money(0)))
    return lines
