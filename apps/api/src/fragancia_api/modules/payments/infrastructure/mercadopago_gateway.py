"""Mercado Pago Checkout Pro adapter: creates a *preference* and answers its redirect URL."""

from typing import Any

import httpx
import structlog

from fragancia_api.modules.payments.application.ports import CheckoutLink, CheckoutRequest
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.shared.kernel import Err, Ok, Result

_log = structlog.get_logger(__name__)

TIMEOUT_SECONDS = 10.0
# Cards only in one installment (decision 4). Plan 003 lifts the first three.
_EXCLUDED_PAYMENT_TYPES = ("ticket", "atm", "bank_transfer")


class MercadoPagoGateway:
    def __init__(
        self,
        *,
        api_url: str,
        access_token: str | None,
        sandbox: bool,
        client: httpx.AsyncClient,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._access_token = access_token
        self._sandbox = sandbox
        self._client = client

    async def create_checkout(
        self, request: CheckoutRequest
    ) -> Result[CheckoutLink, PaymentGatewayUnavailable]:
        if not self._access_token:
            _log.error("payments.gateway_not_configured")
            return Err(PaymentGatewayUnavailable())
        try:
            response = await self._client.post(
                f"{self._api_url}/checkout/preferences",
                json=self._body(request),
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            _log.error("payments.gateway_error", status=None, error=type(error).__name__)
            return Err(PaymentGatewayUnavailable())
        if not response.is_success:
            _log.error("payments.gateway_error", status=response.status_code)
            return Err(PaymentGatewayUnavailable())
        link = self._link(response)
        if link is None:
            _log.error("payments.gateway_error", status=response.status_code, error="bad_body")
            return Err(PaymentGatewayUnavailable())
        return Ok(link)

    def _link(self, response: httpx.Response) -> CheckoutLink | None:
        try:
            data = response.json()
            preference_id = data["id"]
            url = data["sandbox_init_point" if self._sandbox else "init_point"]
        except ValueError, KeyError, TypeError:
            return None
        if not isinstance(preference_id, str) or not isinstance(url, str):
            return None
        if not preference_id or not url:
            return None
        return CheckoutLink(preference_id=preference_id, redirect_url=url)

    @staticmethod
    def _body(request: CheckoutRequest) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "quantity": item.quantity,
                    "unit_price": round(item.unit_price_cents / 100, 2),
                    "currency_id": "MXN",
                }
                for item in request.items
            ],
            "payer": {"name": request.payer_name, "email": request.payer_email},
            "external_reference": str(request.order_id),
            "back_urls": {
                "success": request.back_url,
                "pending": request.back_url,
                "failure": request.back_url,
            },
            "auto_return": "approved",
            "payment_methods": {
                "excluded_payment_types": [{"id": kind} for kind in _EXCLUDED_PAYMENT_TYPES],
                "installments": 1,
            },
            "expires": True,
            "expiration_date_to": request.expires_at.isoformat(),
        }
