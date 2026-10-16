from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, Query, Response

from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    CartOutcome,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.contracts import (
    CartView,
    CheckoutRequest,
    DeliveryOptions,
    DeliveryQuoteResponse,
    PlacedOrder,
    PublicDeliveryOptions,
    SetCartLineRequest,
)
from fragancia_api.modules.orders.http.cart_cookie import CART_COOKIE, CartCookie
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


cart = public_router(prefix="/cart", tags=["orders · cart"])

_CartToken = Annotated[str | None, Cookie(alias=CART_COOKIE)]


def _respond(outcome: CartOutcome, response: Response, cookie: CartCookie) -> CartView:
    if outcome.token is not None:
        cookie.set(response, outcome.token)
    return outcome.view


@cart.get("")
async def get_cart(
    use_case: Annotated[GetCart, Depends(provide(GetCart))],
    token: _CartToken = None,
) -> CartView:
    """The cart behind the `fragancia_cart` cookie, with current prices and availability. No
    cookie, or an unknown one, gives an empty cart and sets no cookie. A line whose presentation
    is sold out, hidden or archived stays in the cart, flagged and left out of the totals
    (decision 22). The cart never reserves stock: only the checkout does."""
    return await use_case.execute(token)


@cart.put(
    "/lines/{presentation_id}",
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def set_cart_line(
    presentation_id: UUID,
    body: SetCartLineRequest,
    response: Response,
    use_case: Annotated[SetCartLine, Depends(provide(SetCartLine))],
    cookie: Annotated[CartCookie, Depends(provide(CartCookie))],
    token: _CartToken = None,
) -> CartView:
    """Set the quantity of a presentation (1 to 10 units, decision 14), creating the cart and
    its cookie when there is none. Changing the quantity acknowledges the current price
    (decision 23). 409 `ORDERS_CART_PRESENTATION_UNAVAILABLE` (unknown or hidden) or
    `ORDERS_CART_STOCK_UNAVAILABLE` (`details.available`, decision 21; in-stock only: made to
    order has no such limit); 422 `ORDERS_CART_QUANTITY_INVALID` or
    `ORDERS_CART_TOO_MANY_LINES`. The cart never reserves stock."""
    outcome = unwrap(await use_case.execute(token, presentation_id, body.quantity))
    return _respond(outcome, response, cookie)


@cart.delete("/lines/{presentation_id}")
async def remove_cart_line(
    presentation_id: UUID,
    response: Response,
    use_case: Annotated[RemoveCartLine, Depends(provide(RemoveCartLine))],
    cookie: Annotated[CartCookie, Depends(provide(CartCookie))],
    token: _CartToken = None,
) -> CartView:
    """Remove a line; removing an absent line changes nothing."""
    outcome = unwrap(await use_case.execute(token, presentation_id))
    return _respond(outcome, response, cookie)


@cart.post("/acknowledge-prices")
async def acknowledge_cart_prices(
    response: Response,
    use_case: Annotated[AcknowledgeCartPrices, Depends(provide(AcknowledgeCartPrices))],
    cookie: Annotated[CartCookie, Depends(provide(CartCookie))],
    token: _CartToken = None,
) -> CartView:
    """The buyer confirms the current prices ("Entendido", decision 23): every line's
    `price_changed_from_cents` clears."""
    outcome = unwrap(await use_case.execute(token))
    return _respond(outcome, response, cookie)


checkout = public_router(prefix="/checkout", tags=["orders · checkout"])


@checkout.post(
    "",
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def place_order(
    body: CheckoutRequest,
    response: Response,
    use_case: Annotated[PlaceOrder, Depends(provide(PlaceOrder))],
    cookie: Annotated[CartCookie, Depends(provide(CartCookie))],
    token: _CartToken = None,
) -> PlacedOrder:
    """Turn the cart behind the `fragancia_cart` cookie into an order. The units in stock are
    reserved for 30 minutes (made-to-order lines reserve nothing, decision 18), the order gets
    the next gapless number `LFI-n` (decision 30) and the cart is emptied and its cookie cleared
    (decision 25). The order waits for payment; if it is not paid in time it is cancelled
    (decision 24). `expected_total_cents` must equal the real total, or nothing is created
    (decision 26). 409: `ORDERS_CHECKOUT_CART_EMPTY`, `ORDERS_CHECKOUT_CART_HAS_PROBLEMS`
    (`details.lines`), `ORDERS_CHECKOUT_CART_CHANGED`, `ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE`
    (`details.method`), `ORDERS_CHECKOUT_TOTAL_CHANGED` (the real figures in `details`) or
    `ORDERS_CHECKOUT_STOCK_UNAVAILABLE` (`details.lines`). 422 `ORDERS_CUSTOMER_DATA_INVALID`
    with `details.field` naming the broken field, or `ORDERS_DELIVERY_OPTIONS_INVALID`."""
    placed = unwrap(await use_case.execute(token, body))
    cookie.clear(response)
    return placed


routers = (admin, public, cart, checkout)
