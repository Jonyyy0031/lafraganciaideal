"""Mercado Pago adapter: creates a Checkout Pro *preference* and answers its redirect URL,
expires a preference, cancels a pending ticket, and fetches a payment's current state."""

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
# Cards, cash in stores (`ticket`) and SPEI (`bank_transfer`), in one installment (decisions 4
# and 14). Bank counters and ATMs stay excluded.
_EXCLUDED_PAYMENT_TYPES = ("atm",)
# The size of `payments.payments.ticket_url` (`String(500)` in `tables.py`).
TICKET_URL_MAX_LENGTH = 500
# The answers to closing a preference or cancelling a ticket that a retry would not change:
# `Ok(False)`, logged. Any other 4xx (401, 403, 408, 429…), a 5xx or a transport error is
# `Err`, so the event retries and, if it never recovers, shows in the failed-events panel.
PERMANENT_REFUSALS = frozenset({400, 404, 409, 422})


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

    async def close_checkout(
        self, preference_id: str, at: datetime
    ) -> Result[bool, PaymentGatewayUnavailable]:
        """Expire a preference at `at` so its link takes no more payments or tickets
        (decision 18). False when Mercado Pago does not know the preference (404)."""
        if not self._access_token:
            _log.error("payments.gateway_not_configured")
            return Err(PaymentGatewayUnavailable())
        stamp = at.isoformat(timespec="milliseconds")
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS, trust_env=False
            ) as client:
                response = await client.put(
                    f"{self._api_url}/checkout/preferences/{preference_id}",
                    # No `date_of_expiration`: Mercado Pago answers `400 invalid date of
                    # expiration` when it is now (plan 003 verification V1).
                    json={"expires": True, "expiration_date_to": stamp},
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
        except httpx.HTTPError as error:
            _log.error("payments.gateway_error", status=None, error=type(error).__name__)
            return Err(PaymentGatewayUnavailable())
        if response.status_code == httpx.codes.NOT_FOUND:
            return Ok(False)
        if response.status_code in PERMANENT_REFUSALS:
            # Retrying the same body would be refused again (plan 003 review L3).
            _log.warning("payments.checkout_close_refused", status=response.status_code)
            return Ok(False)
        if not response.is_success:
            _log.error("payments.gateway_error", status=response.status_code)
            return Err(PaymentGatewayUnavailable())
        return Ok(True)

    async def cancel_payment(self, payment_id: str) -> Result[bool, PaymentGatewayUnavailable]:
        """Cancel a pending cash or transfer ticket so it can no longer be paid (decision 20),
        like the official SDK's `payment/cancel`. False when Mercado Pago refuses it for good
        (`PERMANENT_REFUSALS`: e.g. already paid or already expired)."""
        if not self._access_token:
            _log.error("payments.gateway_not_configured")
            return Err(PaymentGatewayUnavailable())
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS, trust_env=False
            ) as client:
                response = await client.put(
                    f"{self._api_url}/v1/payments/{payment_id}",
                    json={"status": "cancelled"},
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
        except httpx.HTTPError as error:
            _log.error("payments.gateway_error", status=None, error=type(error).__name__)
            return Err(PaymentGatewayUnavailable())
        if response.status_code in PERMANENT_REFUSALS:
            _log.warning(
                "payments.ticket_cancel_refused",
                payment_id=payment_id,
                status=response.status_code,
            )
            return Ok(False)
        if not response.is_success:
            _log.error("payments.gateway_error", status=response.status_code)
            return Err(PaymentGatewayUnavailable())
        return Ok(True)

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
            payment_method = data.get("payment_method_id")
            date_of_expiration = data.get("date_of_expiration")
            details = data.get("transaction_details")
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
        ticket_url = _text(
            details.get("external_resource_url") if isinstance(details, dict) else None
        )
        if ticket_url is not None and len(ticket_url) > TICKET_URL_MAX_LENGTH:
            # Longer than the column: storing it would fail every later notification of the
            # payment, its approval included (plan 003 review L2).
            _log.warning(
                "payments.ticket_url_too_long", payment_id=str(payment_id), length=len(ticket_url)
            )
            ticket_url = None
        return GatewayPayment(
            payment_id=str(payment_id),
            status=status,
            status_detail=status_detail,
            external_reference=reference,
            amount_cents=amount_cents,
            currency=currency,
            payment_type=payment_type,
            approved_at=approved_at,
            payment_method=_text(payment_method),
            expires_at=_datetime(date_of_expiration),
            ticket_url=ticket_url,
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
            # The expiry of a cash or transfer ticket issued from this link (decision 5).
            "date_of_expiration": request.cash_expires_at.isoformat(timespec="milliseconds"),
        }
        # Mercado Pago answers `400 invalid_auto_return` when `auto_return` comes with a success
        # URL that is not https (decision 8). In development the guest returns with the
        # "Volver al sitio" button instead.
        if request.back_url.startswith("https://"):
            body["auto_return"] = "approved"
        return body


def _text(value: object) -> str | None:
    """An optional text field: a wrong type or an empty string is None."""
    return value if isinstance(value, str) and value else None


def _datetime(value: object) -> datetime | None:
    """An optional ISO 8601 timestamp with its offset: a wrong type, a bad value or a missing
    offset is None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
