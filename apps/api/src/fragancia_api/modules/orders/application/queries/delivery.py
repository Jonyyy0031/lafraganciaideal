from fragancia_api.modules.orders.application.ports import DeliverySettingsReader
from fragancia_api.modules.orders.contracts import (
    DeliveryOptions,
    DeliveryQuoteLine,
    DeliveryQuoteResponse,
    HomeDeliveryOptions,
    LocalDeliveryOptions,
    PickupOptions,
    PublicDeliveryOptions,
    PublicHomeDelivery,
    PublicLocalDelivery,
    PublicPickup,
)
from fragancia_api.modules.orders.domain.delivery import (
    PostalCode,
    Subtotal,
    quote,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


def _sorted_codes(codes: frozenset[PostalCode]) -> list[str]:
    return sorted(code.value for code in codes)


class GetDeliveryOptions:
    """Query: the full configuration, for the panel."""

    def __init__(self, reader: DeliverySettingsReader) -> None:
        self._reader = reader

    async def execute(self) -> DeliveryOptions:
        settings = await self._reader.current()
        return DeliveryOptions(
            home=HomeDeliveryOptions(
                enabled=settings.home.enabled,
                price_cents=settings.home.price.money.cents,
                free_from_cents=(
                    settings.home.free_from.cents if settings.home.free_from is not None else None
                ),
            ),
            local=LocalDeliveryOptions(
                enabled=settings.local.enabled,
                price_cents=settings.local.price.money.cents,
                postal_codes=_sorted_codes(settings.local.postal_codes),
            ),
            pickup=PickupOptions(
                enabled=settings.pickup.enabled,
                address=settings.pickup.address,
                hours=settings.pickup.hours,
            ),
        )


class GetPublicDeliveryOptions:
    """Query: the enabled methods only, for the storefront."""

    def __init__(self, reader: DeliverySettingsReader) -> None:
        self._reader = reader

    async def execute(self) -> PublicDeliveryOptions:
        settings = await self._reader.current()
        home = settings.home
        local = settings.local
        pickup = settings.pickup
        return PublicDeliveryOptions(
            home=(
                PublicHomeDelivery(
                    price_cents=home.price.money.cents,
                    free_from_cents=home.free_from.cents if home.free_from is not None else None,
                )
                if home.enabled
                else None
            ),
            local=(
                PublicLocalDelivery(
                    price_cents=local.price.money.cents,
                    postal_codes=_sorted_codes(local.postal_codes),
                )
                if local.enabled
                else None
            ),
            pickup=(
                PublicPickup(address=pickup.address, hours=pickup.hours) if pickup.enabled else None
            ),
        )


class QuoteDelivery:
    """Query: the methods that apply to a postal code and a subtotal, with their cost."""

    def __init__(self, reader: DeliverySettingsReader) -> None:
        self._reader = reader

    async def execute(
        self, postal_code: str | None, subtotal_cents: int
    ) -> Result[DeliveryQuoteResponse, DomainError]:
        code: PostalCode | None = None
        if postal_code is not None:
            match PostalCode.create(postal_code):
                case Err(invalid):
                    return Err(invalid)
                case Ok(parsed):
                    code = parsed
        match Subtotal.create(subtotal_cents):
            case Err(bad_subtotal):
                return Err(bad_subtotal)
            case Ok(subtotal):
                pass
        lines = quote(await self._reader.current(), code, subtotal)
        return Ok(
            DeliveryQuoteResponse(
                options=[
                    DeliveryQuoteLine(method=line.method.value, cost_cents=line.cost.cents)
                    for line in lines
                ]
            )
        )
