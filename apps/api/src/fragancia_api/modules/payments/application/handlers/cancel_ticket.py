"""Cancel a cash or transfer ticket that arrived after its order stopped waiting for payment
(decision 20, late notifications; plan 003 review R2)."""

from fragancia_api.modules.payments.application.ports import PaymentGateway
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.kernel import Err, Ok


class CancelTicket:
    """`payments.ticket.cancel_requested`: cancel the ticket in Mercado Pago so it can no longer
    be paid.

    Idempotent: cancelling a ticket again changes nothing. Mercado Pago down raises, so the
    relay retries this event alone; a permanent refusal (e.g. the ticket was already paid or
    expired) is logged by the gateway. The payment's row is not edited here: Mercado Pago's
    notification of the cancelled status refreshes it, and a ticket paid before the cancel
    arrives `approved` (`refund_due`, plan 002).
    """

    def __init__(self, *, gateway: PaymentGateway) -> None:
        self._gateway = gateway

    async def __call__(self, message: EventMessage) -> None:
        payment_id = str(message.payload["payment_id"])
        match await self._gateway.cancel_payment(payment_id):
            case Err(_):
                raise RuntimeError("payments.gateway_unavailable")
            case Ok(_):
                pass
