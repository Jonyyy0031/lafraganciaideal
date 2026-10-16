from typing import Annotated

from fastapi import Depends, Query

from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.contracts import (
    DeliveryOptions,
    DeliveryQuoteResponse,
    PublicDeliveryOptions,
)
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    public_router,
    require_permission,
    unwrap,
)

# Permissions are atomic strings owned by identity (modules may not import each other).
DELIVERY_MANAGE = "delivery:manage"

admin = admin_router(
    prefix="/delivery-options",
    tags=["orders · admin"],
    dependencies=[Depends(require_permission(DELIVERY_MANAGE))],
)
public = public_router(prefix="/delivery-options", tags=["orders"])


@admin.get("")
async def get_delivery_options(
    use_case: Annotated[GetDeliveryOptions, Depends(provide(GetDeliveryOptions))],
) -> DeliveryOptions:
    """The delivery configuration: home, local and pickup, each with its on/off switch. Pickup
    is always free; home delivery is free from `free_from_cents` of subtotal when that is set."""
    return await use_case.execute()


@admin.put("", status_code=204, responses={422: {"model": ErrorResponse}})
async def update_delivery_options(
    body: DeliveryOptions,
    use_case: Annotated[UpdateDeliveryOptions, Depends(provide(UpdateDeliveryOptions))],
) -> None:
    """Replace the whole delivery configuration. Pickup is always free; home delivery is free
    from `free_from_cents` of subtotal when that is set. 422 `ORDERS_DELIVERY_OPTIONS_INVALID`
    with `details.field` naming the broken field."""
    unwrap(await use_case.execute(body))


@public.get("")
async def get_public_delivery_options(
    use_case: Annotated[GetPublicDeliveryOptions, Depends(provide(GetPublicDeliveryOptions))],
) -> PublicDeliveryOptions:
    """The enabled delivery methods only (a disabled one is `null`). Pickup is always free; home
    delivery is free from `free_from_cents` of subtotal when that is set."""
    return await use_case.execute()


@public.get("/quote", responses={422: {"model": ErrorResponse}})
async def quote_delivery(
    use_case: Annotated[QuoteDelivery, Depends(provide(QuoteDelivery))],
    subtotal_cents: Annotated[int, Query()],
    postal_code: Annotated[str | None, Query()] = None,
) -> DeliveryQuoteResponse:
    """The methods that apply, in the order home, local, pickup, with their cost. Pickup is
    free; home delivery is free when the subtotal reaches `free_from_cents`; local delivery
    applies only when the postal code is on its list (without a postal code it never does). 422
    `ORDERS_POSTAL_CODE_INVALID` or `ORDERS_DELIVERY_OPTIONS_INVALID` (`field: subtotal_cents`)."""
    return unwrap(await use_case.execute(postal_code, subtotal_cents))


routers = (admin, public)
