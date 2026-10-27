"""Plan 002 step 4, application: `SendFlaggedPaymentToOwners` (flagged_payment_email.py): the
copy, one email per active owner, the no-owner path, a rejected address and the retry on a
send failure. The payload is built from the real `PaymentFlagged` event."""

from uuid import UUID

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.handlers.flagged_payment_email import (
    SendFlaggedPaymentToOwners,
    money,
)
from fragancia_api.modules.payments.application.ports import OwnerContact
from fragancia_api.modules.payments.domain.events import PaymentFlagged
from fragancia_api.shared.application.email import EmailMessage, EmailRejected
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import RecordingEmailSender
from tests.unit.payments.support import NOW

ANA = OwnerContact(name="Ana", email="ana@owners.test")
LUIS = OwnerContact(name="Luis", email="luis@owners.test")


class FakeOwners:
    def __init__(self, *owners: OwnerContact) -> None:
        self.owners = list(owners)

    async def active_owners(self) -> list[OwnerContact]:
        return list(self.owners)


def flagged(
    reason: str = "amount_mismatch",
    *,
    payment_id: str = "555000111",
    number: int = 1007,
    amount_cents: int = 123_450,
    currency: str = "MXN",
) -> EventMessage:
    event = PaymentFlagged(
        payment_id=payment_id,
        number=number,
        reason=reason,
        amount_cents=amount_cents,
        currency=currency,
    )
    return EventMessage(id=UUID(int=2), name=event.name, payload=event.payload(), occurred_at=NOW)


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "$0.00 MXN"),
        (5, "$0.05 MXN"),
        (100, "$1.00 MXN"),
        (123_450, "$1,234.50 MXN"),
        (268_000, "$2,680.00 MXN"),
        (100_000_050, "$1,000,000.50 MXN"),
    ],
)
def test_money_is_formatted_with_thousands_cents_and_the_currency(cents: int, text: str) -> None:
    assert money(cents, "MXN") == text


async def test_an_amount_mismatch_emails_the_owner_with_the_exact_copy() -> None:
    email = RecordingEmailSender()

    await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA), email=email)(flagged())

    assert email.sent == [
        EmailMessage(
            to="ana@owners.test",
            subject="Pago a revisar: LFI-1007",
            body=(
                "Hola Ana:\n\n"
                "Mercado Pago aprobó el pago 555000111 por $1,234.50 MXN del pedido LFI-1007, "
                "pero el monto no coincide con el total del pedido, así que el pedido sigue "
                "pendiente.\n\n"
                "Revísalo en Mercado Pago; el reembolso automático llegará en una versión "
                "próxima.\n"
            ),
        )
    ]


async def test_a_stock_gone_payment_says_the_order_was_cancelled() -> None:
    email = RecordingEmailSender()

    await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA), email=email)(flagged("stock_gone"))

    [sent] = email.sent
    assert sent.subject == "Pago a revisar: LFI-1007"
    assert "pero llegó cuando ya no había existencias, así que el pedido se canceló." in sent.body
    assert "sigue pendiente" not in sent.body


async def test_every_active_owner_gets_their_own_email_greeting_them_by_name() -> None:
    email = RecordingEmailSender()

    await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA, LUIS), email=email)(flagged())

    assert [(m.to, m.body.split("\n")[0]) for m in email.sent] == [
        ("ana@owners.test", "Hola Ana:"),
        ("luis@owners.test", "Hola Luis:"),
    ]
    assert {m.subject for m in email.sent} == {"Pago a revisar: LFI-1007"}


async def test_the_money_carries_the_currency_the_payment_was_charged_in() -> None:
    email = RecordingEmailSender()

    await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA), email=email)(
        flagged(amount_cents=5000, currency="USD")
    )

    assert "por $50.00 USD del pedido" in email.sent[0].body


async def test_without_active_owners_nothing_is_sent_and_it_is_logged() -> None:
    email = RecordingEmailSender()

    with capture_logs() as logs:
        await SendFlaggedPaymentToOwners(owners=FakeOwners(), email=email)(flagged())

    assert email.sent == []
    assert logs == [
        {
            "event": "payments.flagged_email_no_owner",
            "payment_id": "555000111",
            "number": 1007,
            "log_level": "warning",
        }
    ]


async def test_a_rejected_address_is_logged_and_the_others_still_get_the_email() -> None:
    class RejectingFirst(RecordingEmailSender):
        async def send(self, message: EmailMessage) -> None:
            if message.to == ANA.email:
                raise EmailRejected("bad address")
            await super().send(message)

    email = RejectingFirst()

    with capture_logs() as logs:
        await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA, LUIS), email=email)(flagged())

    assert [m.to for m in email.sent] == [LUIS.email]
    assert [(e["event"], e["address"]) for e in logs] == [
        ("payments.flagged_email_rejected", ANA.email)
    ]


async def test_a_send_failure_raises_so_the_relay_retries() -> None:
    email = RecordingEmailSender()
    email.fail = True

    with pytest.raises(ConnectionError):
        await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA), email=email)(flagged())


async def test_an_unknown_reason_is_a_programming_error_not_an_email() -> None:
    email = RecordingEmailSender()

    with pytest.raises(KeyError):
        await SendFlaggedPaymentToOwners(owners=FakeOwners(ANA), email=email)(flagged("other"))

    assert email.sent == []
