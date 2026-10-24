from typing import Annotated

from fastapi import Depends

from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.contracts import StartCheckoutRequest, StartCheckoutResponse
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
    """The Mercado Pago URL that pays an order (decisions 2 and 4, ADR 0004). The order's
    private-link token is a secret and travels in the body, never in a URL. Only cards, in one
    installment. The link expires with the order's payment window, and a second request reuses
    it while it is valid. The browser's return from Mercado Pago never confirms a payment.
    404 `PAYMENTS_ORDER_NOT_FOUND`; 409 `PAYMENTS_ORDER_NOT_PAYABLE` (`details.status`: the
    order is not waiting for payment or its window passed); 503
    `PAYMENTS_GATEWAY_UNAVAILABLE` (Mercado Pago failed or is not configured)."""
    redirect_url = unwrap(await use_case.execute(body.access_token))
    return StartCheckoutResponse(redirect_url=redirect_url)


routers = (payments,)
