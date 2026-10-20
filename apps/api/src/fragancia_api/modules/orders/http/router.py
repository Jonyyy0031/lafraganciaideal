from typing import Annotated, Any
from uuid import UUID

from fastapi import Cookie, Depends, Path, Query, Request, Response

from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    CartOutcome,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.application.commands.customer_order import (
    CancelOrderByCustomer,
    LookUpOrder,
)
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.commands.lifecycle import (
    AddOrderNote,
    AdvanceOrder,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderPaid,
    MarkOrderRefunded,
)
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.application.queries.customer_order import GetCustomerOrder
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.application.queries.orders import GetOrder, ListOrders
from fragancia_api.modules.orders.contracts import (
    AdminOrderDetail,
    AdminOrderPage,
    AdvanceOrderRequest,
    CancelOrderRequest,
    CartView,
    CheckoutRequest,
    CustomerOrderView,
    DeliveryOptions,
    DeliveryQuoteResponse,
    MarkPaidRequest,
    OrderAccessRequest,
    OrderLookupRequest,
    OrderLookupResponse,
    OrderNoteRequest,
    OrderStatusName,
    PlacedOrder,
    PublicDeliveryOptions,
    RefundDoneRequest,
    SetCartLineRequest,
)
from fragancia_api.modules.orders.domain.errors import OrderNotFound
from fragancia_api.modules.orders.domain.order import parse_order_number
from fragancia_api.modules.orders.http.cart_cookie import CART_COOKIE, CartCookie
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    public_router,
    require_permission,
    unwrap,
)
from fragancia_api.shared.kernel import Err, Ok

# Permissions are atomic strings owned by identity (modules may not import each other).
DELIVERY_MANAGE = "delivery:manage"
ORDERS_MANAGE = "orders:manage"
ORDERS_FINANCE = "orders:finance"

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
    with `details.field` naming the broken field, or `ORDERS_DELIVERY_OPTIONS_INVALID`.

    Retrying after a lost response (decision 31): when the cart cookie's cart is gone because an
    order was placed from it in the last 30 minutes and is still waiting for payment, the same
    201 is answered again (same number, same `access_token`, no second order or reservation).
    The request body is ignored in that case."""
    placed = unwrap(await use_case.execute(token, body))
    cookie.clear(response)
    return placed


orders_admin = admin_router(
    prefix="/orders",
    tags=["orders · admin"],
    dependencies=[Depends(require_permission(ORDERS_MANAGE))],
)
_finance = [Depends(require_permission(ORDERS_FINANCE))]

type _Responses = dict[int | str, dict[str, Any]]

_404: _Responses = {404: {"model": ErrorResponse}}
_404_409: _Responses = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}}
_404_409_422: _Responses = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_404_422: _Responses = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


def _number(raw: str) -> int:
    """`LFI-1001` or `1001`; anything else is an unknown order (404)."""
    number = parse_order_number(raw)
    return unwrap(Ok(number) if number is not None else Err(OrderNotFound()))


def _actor(request: Request) -> str:
    actor_id: str = request.state.actor.id
    return actor_id


_Number = Annotated[str, Path(description="`LFI-1001` or `1001`")]


@orders_admin.get("")
async def list_orders(
    use_case: Annotated[ListOrders, Depends(provide(ListOrders))],
    status: OrderStatusName | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AdminOrderPage:
    """Orders, newest number first. `status` keeps one status; `q` matches an order number
    (`LFI-1001` or `1001`), an email fragment or a name fragment, ignoring case. `orders:manage`."""
    return await use_case.execute(status=status, q=q, page=page, size=size)


@orders_admin.get("/{number}", responses=_404)
async def get_order(
    number: _Number,
    use_case: Annotated[GetOrder, Depends(provide(GetOrder))],
) -> AdminOrderDetail:
    """One order with its lines, address, pickup point, invoice data, history (oldest first),
    notes (newest first), refund status, carrier, tracking number and reservation id. 404
    `ORDERS_ORDER_NOT_FOUND`. `orders:manage`."""
    return unwrap(await use_case.execute(_number(number)))


@orders_admin.post(
    "/{number}/payment",
    status_code=204,
    dependencies=_finance,
    responses=_404_409_422,
)
async def mark_order_paid(
    number: _Number,
    body: MarkPaidRequest,
    request: Request,
    use_case: Annotated[MarkOrderPaid, Depends(provide(MarkOrderPaid))],
) -> None:
    """Mark a `pending_payment` order as paid by hand (decision 32); the note says how, 1 to 300
    characters. Sells the reserved units. Owners only (`orders:finance`, decision 34). 404
    `ORDERS_ORDER_NOT_FOUND`; 409 `ORDERS_TRANSITION_INVALID` (`details.from`, `details.to`) or
    inventory's `INVENTORY_STOCK_UNAVAILABLE` / `INVENTORY_STOCK_INSUFFICIENT` when the units are
    gone (the order stays unpaid); 422 `ORDERS_ORDER_DATA_INVALID` (`details.field`)."""
    unwrap(await use_case.execute(_number(number), body.note, _actor(request)))


@orders_admin.post("/{number}/advance", status_code=204, responses=_404_409_422)
async def advance_order(
    number: _Number,
    body: AdvanceOrderRequest,
    request: Request,
    use_case: Annotated[AdvanceOrder, Depends(provide(AdvanceOrder))],
) -> None:
    """Move a paid order forward (decisions 9, 33 and 35): `preparing` (optional), then
    `shipped` (home and local delivery) or `ready_for_pickup` (pickup), then `delivered`.
    Shipping a home delivery needs `carrier` and `tracking_number` (1 to 60 characters each);
    local delivery takes neither. States never move backwards. 404 `ORDERS_ORDER_NOT_FOUND`;
    409 `ORDERS_TRANSITION_INVALID` (`details.from`, `details.to`); 422
    `ORDERS_ORDER_DATA_INVALID` (`details.field`). `orders:manage`."""
    unwrap(
        await use_case.execute(
            _number(number), body.to, body.carrier, body.tracking_number, _actor(request)
        )
    )


@orders_admin.post("/{number}/cancel", status_code=204, responses=_404_409_422)
async def cancel_order(
    number: _Number,
    body: CancelOrderRequest,
    request: Request,
    use_case: Annotated[CancelUnpaidOrder, Depends(provide(CancelUnpaidOrder))],
) -> None:
    """Cancel an order that is still `pending_payment` (decision 10) with a reason of 1 to 300
    characters; its reserved units are released. A paid order uses `cancel-paid`. 404
    `ORDERS_ORDER_NOT_FOUND`; 409 `ORDERS_TRANSITION_INVALID`; 422 `ORDERS_ORDER_DATA_INVALID`
    (`details.field`). `orders:manage`."""
    unwrap(await use_case.execute(_number(number), body.reason, _actor(request)))


@orders_admin.post(
    "/{number}/cancel-paid",
    status_code=204,
    dependencies=_finance,
    responses=_404_409_422,
)
async def cancel_paid_order(
    number: _Number,
    body: CancelOrderRequest,
    request: Request,
    use_case: Annotated[CancelPaidOrder, Depends(provide(CancelPaidOrder))],
) -> None:
    """Cancel a `paid` order that is not yet prepared or shipped (decisions 9 and 36). The sold
    units go back on hand and the refund is left pending (`refund_status: pending`). Owners only
    (`orders:finance`, decision 34). 404 `ORDERS_ORDER_NOT_FOUND`; 409
    `ORDERS_TRANSITION_INVALID`; 422 `ORDERS_ORDER_DATA_INVALID` (`details.field`)."""
    unwrap(await use_case.execute(_number(number), body.reason, _actor(request)))


@orders_admin.post(
    "/{number}/refund",
    status_code=204,
    dependencies=_finance,
    responses=_404_409_422,
)
async def mark_order_refunded(
    number: _Number,
    body: RefundDoneRequest,
    request: Request,
    use_case: Annotated[MarkOrderRefunded, Depends(provide(MarkOrderRefunded))],
) -> None:
    """Record that the refund of a cancelled paid order was made by hand (decision 36); the
    note is 1 to 300 characters. Owners only (`orders:finance`, decision 34). 404
    `ORDERS_ORDER_NOT_FOUND`; 409 `ORDERS_TRANSITION_INVALID` (not cancelled, or no pending
    refund); 422 `ORDERS_ORDER_DATA_INVALID` (`details.field`)."""
    unwrap(await use_case.execute(_number(number), body.note, _actor(request)))


@orders_admin.post("/{number}/notes", status_code=201, responses=_404_422)
async def add_order_note(
    number: _Number,
    body: OrderNoteRequest,
    request: Request,
    use_case: Annotated[AddOrderNote, Depends(provide(AddOrderNote))],
) -> None:
    """Add an internal note (1 to 1000 characters) signed with the admin's user id. It never
    waits on a move in flight (the order row is locked `FOR NO KEY UPDATE`). 404
    `ORDERS_ORDER_NOT_FOUND`; 422 `ORDERS_ORDER_DATA_INVALID`
    (`details.field`). `orders:manage`."""
    unwrap(await use_case.execute(_number(number), body.text, _actor(request)))


customer_orders = public_router(prefix="/orders", tags=["orders · customer"])


@customer_orders.post(
    "/lookup",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def look_up_order(
    body: OrderLookupRequest,
    request: Request,
    use_case: Annotated[LookUpOrder, Depends(provide(LookUpOrder))],
) -> OrderLookupResponse:
    """Find an order again with its number (`LFI-1001` or `1001`) and the email it was placed
    with (any case, spaces trimmed); answers the private-link token (decision 39). The token is a
    secret that opens the order. A wrong email and an unknown number answer the same 404
    `ORDERS_LOOKUP_NOT_FOUND`. Attempts are limited per IP and per order number: 429
    `ORDERS_LOOKUP_RATE_LIMITED`."""
    token = unwrap(
        await use_case.execute(
            body.number, body.email, request.client.host if request.client else None
        )
    )
    return OrderLookupResponse(access_token=token)


@customer_orders.post("/view", responses=_404)
async def get_customer_order(
    body: OrderAccessRequest,
    use_case: Annotated[GetCustomerOrder, Depends(provide(GetCustomerOrder))],
) -> CustomerOrderView:
    """The customer's page of an order, by the private-link token from checkout (decisions 13,
    40 and 42). The token is a secret: whoever has it sees the order, with the name, phone,
    address and invoice data. It travels in the body, never in a URL path, so no access or error
    log records it; the web link carries it in the URL fragment (`/pedido#token=...`) and the
    page POSTs it, as ADR 0010 does for identity's links. No internal notes, admin ids or cancel
    reason. 404 `ORDERS_ORDER_NOT_FOUND`."""
    return unwrap(await use_case.execute(body.access_token))


@customer_orders.post("/cancel", status_code=204, responses=_404_409)
async def cancel_order_by_customer(
    body: OrderAccessRequest,
    use_case: Annotated[CancelOrderByCustomer, Depends(provide(CancelOrderByCustomer))],
) -> None:
    """The customer cancels their own order while it is `pending_payment` (decisions 41 and 42);
    its reserved units are released. The token is a secret and travels in the body (see
    `POST /orders/view`). 404 `ORDERS_ORDER_NOT_FOUND`; 409 `ORDERS_TRANSITION_INVALID` once the
    order is paid or cancelled."""
    unwrap(await use_case.execute(body.access_token))


routers = (admin, public, cart, checkout, orders_admin, customer_orders)
