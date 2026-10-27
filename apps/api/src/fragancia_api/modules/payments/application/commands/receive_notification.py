import re

import structlog

from fragancia_api.modules.payments.domain.errors import (
    PaymentGatewayUnavailable,
    PaymentWebhookSignatureInvalid,
)
from fragancia_api.modules.payments.domain.events import PaymentNotificationReceived
from fragancia_api.modules.payments.domain.webhook_signature import signature_valid
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

_log = structlog.get_logger(__name__)

PAYMENT_KIND = "payment"
_PAYMENT_ID = re.compile(r"[0-9]{1,20}")


class ReceivePaymentNotification:
    """Command: accept a Mercado Pago webhook (ADR 0004, ADR 0006).

    Other kinds (`merchant_order`, legacy IPN) and malformed ids are acknowledged and ignored
    first, `Ok(False)`, without looking at the signature: they store nothing. A `payment`
    notification must be signed, and is then stored as a `payments.notification.received`
    outbox event; the worker fetches the payment from Mercado Pago and applies it (the body is
    never trusted).
    """

    def __init__(
        self,
        *,
        secret: str | None,
        events: EventPublisher,
        transactions: TransactionRunner,
    ) -> None:
        self._secret = secret
        self._events = events
        self._transactions = transactions

    async def execute(
        self,
        x_signature: str | None,
        x_request_id: str | None,
        data_id: str | None,
        kind: str | None,
    ) -> Result[bool, DomainError]:
        # Ignored notifications do nothing, so they need no signature: a legacy IPN request
        # carries none and would otherwise be retried forever (plan 002 review L3).
        if kind != PAYMENT_KIND or data_id is None or _PAYMENT_ID.fullmatch(data_id) is None:
            _log.info("payments.webhook_ignored", kind=kind, x_request_id=x_request_id)
            return Ok(False)
        if not self._secret:
            _log.error("payments.webhook_not_configured")
            return Err(PaymentGatewayUnavailable())
        if not signature_valid(self._secret, x_signature, x_request_id, data_id):
            _log.warning("payments.webhook_signature_invalid", x_request_id=x_request_id)
            return Err(PaymentWebhookSignatureInvalid())
        payment_id = data_id

        async def store() -> Result[bool, DomainError]:
            await self._events.publish([PaymentNotificationReceived(payment_id=payment_id)])
            return Ok(True)

        return await self._transactions.run(store)
