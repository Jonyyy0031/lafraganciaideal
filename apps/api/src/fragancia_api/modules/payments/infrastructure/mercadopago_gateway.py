"""Mercado Pago adapter: creates a Checkout Pro *preference* and answers its redirect URL, and
fetches a payment's current state."""

from datetime import datetime
from typing import Any

import httpx
import structlog

from fragancia_api.modules.payments.application.ports import (
    CheckoutLink,
    CheckoutRequest,
    GatewayPayment,
)
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.modules.payments.domain.payment import amount_to_cents
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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._access_token = access_token
        self._sandbox = sandbox
        self._transport = transport

    async def create_checkout(
        self, request: CheckoutRequest
    ) -> Result[CheckoutLink, PaymentGatewayUnavailable]:
        if not self._access_token:
            _log.error("payments.gateway_not_configured")
            return Err(PaymentGatewayUnavailable())
        try:
            # One client per call, like the catalog downloader: nothing long-lived to close,
            # and the proxy environment variables are ignored.
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS, trust_env=False
            ) as client:
                response = await client.post(
                    f"{self._api_url}/checkout/preferences",
                    json=self._body(request),
                    headers={"Authorization": f"Bearer {self._access_token}"},
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

    async def get_payment(
        self, payment_id: str
    ) -> Result[GatewayPayment | None, PaymentGatewayUnavailable]:
        if not self._access_token:
            _log.error("payments.gateway_not_configured")
            return Err(PaymentGatewayUnavailable())
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS, trust_env=False
            ) as client:
                response = await client.get(
                    f"{self._api_url}/v1/payments/{payment_id}",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
        except httpx.HTTPError as error:
            _log.error("payments.gateway_error", status=None, error=type(error).__name__)
            return Err(PaymentGatewayUnavailable())
        if response.status_code == httpx.codes.NOT_FOUND:
            return Ok(None)
        if not response.is_success:
            _log.error("payments.gateway_error", status=response.status_code)
            return Err(PaymentGatewayUnavailable())
        payment = self._payment(response)
        if payment is None:
            _log.error("payments.gateway_error", status=response.status_code, error="bad_body")
            return Err(PaymentGatewayUnavailable())
        return Ok(payment)

    @staticmethod
    def _payment(response: httpx.Response) -> GatewayPayment | None:
        try:
            data = response.json()
            payment_id = data["id"]
            status = data["status"]
            amount = data["transaction_amount"]
            currency = data["currency_id"]
            status_detail = data.get("status_detail")
            reference = data.get("external_reference")
            payment_type = data.get("payment_type_id")
            date_approved = data.get("date_approved")
        except ValueError, KeyError, TypeError, AttributeError:
            return None
        if isinstance(payment_id, bool) or not isinstance(payment_id, int | str):
            return None
        if isinstance(amount, bool) or not isinstance(amount, int | float):
            return None
        if not isinstance(status, str) or not status or not isinstance(currency, str):
            return None
        optional = (status_detail, reference, payment_type, date_approved)
        if not all(value is None or isinstance(value, str) for value in optional):
            return None
        try:
            approved_at = datetime.fromisoformat(date_approved) if date_approved else None
            amount_cents = amount_to_cents(amount)
        except ValueError, ArithmeticError:
            return None
        return GatewayPayment(
            payment_id=str(payment_id),
            status=status,
            status_detail=status_detail,
            external_reference=reference,
            amount_cents=amount_cents,
            currency=currency,
            payment_type=payment_type,
            approved_at=approved_at,
        )

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
        body: dict[str, Any] = {
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
            "payment_methods": {
                "excluded_payment_types": [{"id": kind} for kind in _EXCLUDED_PAYMENT_TYPES],
                "installments": 1,
            },
            "expires": True,
            "expiration_date_to": request.expires_at.isoformat(timespec="milliseconds"),
        }
        # Mercado Pago answers `400 invalid_auto_return` when `auto_return` comes with a success
        # URL that is not https (decision 8). In development the guest returns with the
        # "Volver al sitio" button instead.
        if request.back_url.startswith("https://"):
            body["auto_return"] = "approved"
        return body
