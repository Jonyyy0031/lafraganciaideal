from typing import Annotated

from fastapi import Depends, Header, Query

from fragancia_api.modules.payments.application.commands.receive_notification import (
    ReceivePaymentNotification,
)
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.application.queries.pending_tickets import GetPendingTickets
from fragancia_api.modules.payments.contracts import (
    PendingTickets,
    PendingTicketsRequest,
    StartCheckoutRequest,
    StartCheckoutResponse,
    WebhookAck,
)
from fragancia_api.shared.http import ErrorResponse, provide, public_router, unwrap

payments = public_router(prefix="/payments", tags=["payments"])


@payments.post(
    "/checkout",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def start_checkout(
    body: StartCheckoutRequest,
    use_case: Annotated[StartCheckout, Depends(provide(StartCheckout))],
) -> StartCheckoutResponse:
    """The Mercado Pago URL that pays an order (decisions 2, 4 and 14, ADR 0004). The order's
    private-link token is a secret and travels in the body, never in a URL. Cards in one
    installment, cash in stores and SPEI; a cash or transfer ticket issued from the link
    expires 48 hours after the link was created. The link expires with the order's payment
    window, and a second request reuses it while it is valid. The browser's return from
    Mercado Pago never confirms a payment.
    404 `PAYMENTS_ORDER_NOT_FOUND`; 409 `PAYMENTS_ORDER_NOT_PAYABLE` (`details.status`: the
    order is not waiting for payment or its window passed); 503
    `PAYMENTS_GATEWAY_UNAVAILABLE` (Mercado Pago failed or is not configured)."""
    redirect_url = unwrap(await use_case.execute(body.access_token))
    return StartCheckoutResponse(redirect_url=redirect_url)


@payments.post("/pending", responses={404: {"model": ErrorResponse}})
async def list_pending_tickets(
    body: PendingTicketsRequest,
    use_case: Annotated[GetPendingTickets, Depends(provide(GetPendingTickets))],
) -> PendingTickets:
    """The order's cash and transfer tickets that can still be paid (decision 16): pending,
    not expired, newest first, each with its method, amount, expiry and Mercado Pago's
    instructions page. The order's private-link token travels in the body, never in a URL.
    An order that is not waiting for payment answers no items. 404
    `PAYMENTS_ORDER_NOT_FOUND`."""
    return unwrap(await use_case.execute(body.access_token))


@payments.post(
    "/webhooks/mercadopago",
    responses={
        401: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def receive_mercadopago_webhook(
    use_case: Annotated[ReceivePaymentNotification, Depends(provide(ReceivePaymentNotification))],
    data_id: Annotated[str | None, Query(alias="data.id")] = None,
    kind: Annotated[str | None, Query(alias="type")] = None,
    x_signature: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> WebhookAck:
    """Mercado Pago's webhook (ADR 0004: payments are confirmed only by verified webhooks).
    Called by Mercado Pago, not by the storefront. The `x-signature` header is checked against
    the webhook secret over `data.id` (query), `x-request-id` and its timestamp; the body is not
    read. A `type=payment` notification is queued and the worker fetches the payment from
    Mercado Pago and applies it; other types are acknowledged and ignored. 200 `received`;
    401 `PAYMENTS_WEBHOOK_SIGNATURE_INVALID`; 503 `PAYMENTS_GATEWAY_UNAVAILABLE` (no webhook
    secret configured)."""
    unwrap(await use_case.execute(x_signature, x_request_id, data_id, kind))
    return WebhookAck()


routers = (payments,)
