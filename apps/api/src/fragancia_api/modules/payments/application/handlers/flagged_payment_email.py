"""`payments.payment.flagged`: tell the owners about an approved payment to review (decisions 9
and 10).

Runs in the worker, inside the relay's transaction. Plain text, in Spanish. A send failure
raises, so the relay retries the event (at-least-once: a retry can send the same email twice).
An address the mail server permanently rejects (`EmailRejected`) is logged and skipped.
"""

import structlog

from fragancia_api.modules.payments.application.ports import OwnerContacts
from fragancia_api.shared.application.email import EmailMessage, EmailRejected, EmailSender
from fragancia_api.shared.application.events import EventMessage

_log = structlog.get_logger(__name__)

SUBJECT = "Pago a revisar: LFI-{number}"
BODY = (
    "Hola {name}:\n\n"
    "Mercado Pago aprobó el pago {payment_id} por {money} del pedido LFI-{number}, "
    "pero {reason}.\n\n"
    "Revísalo en Mercado Pago; el reembolso automático llegará en una versión próxima.\n"
)
REASONS = {
    "amount_mismatch": (
        "el monto no coincide con el total del pedido, así que el pedido sigue pendiente"
    ),
    "stock_gone": "llegó cuando ya no había existencias, así que el pedido se canceló",
}


def money(cents: int, currency: str) -> str:
    """`$1,234.50 MXN`."""
    return f"${cents // 100:,}.{cents % 100:02d} {currency}"


class SendFlaggedPaymentToOwners:
    """One email per active owner, like orders' paid-order email."""

    def __init__(self, *, owners: OwnerContacts, email: EmailSender) -> None:
        self._owners = owners
        self._email = email

    async def __call__(self, message: EventMessage) -> None:
        payload = message.payload
        payment_id = str(payload["payment_id"])
        number = int(str(payload["number"]))
        reason = REASONS[str(payload["reason"])]
        amount = money(int(str(payload["amount_cents"])), str(payload["currency"]))
        owners = await self._owners.active_owners()
        if not owners:
            _log.warning("payments.flagged_email_no_owner", payment_id=payment_id, number=number)
            return
        subject = SUBJECT.format(number=number)
        for owner in owners:
            body = BODY.format(
                name=owner.name,
                payment_id=payment_id,
                money=amount,
                number=number,
                reason=reason,
            )
            try:
                await self._email.send(EmailMessage(to=owner.email, subject=subject, body=body))
            except EmailRejected:
                _log.warning("payments.flagged_email_rejected", address=owner.email)
