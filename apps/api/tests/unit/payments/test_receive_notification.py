"""Plan 002 step 4, application: `ReceivePaymentNotification` (receive_notification.py): the
secret, the signature, ignored kinds and malformed ids, and the one outbox event."""

import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.commands.receive_notification import (
    ReceivePaymentNotification,
)
from fragancia_api.modules.payments.domain.errors import (
    PaymentGatewayUnavailable,
    PaymentWebhookSignatureInvalid,
)
from fragancia_api.modules.payments.domain.events import PaymentNotificationReceived
from fragancia_api.shared.infrastructure.in_memory import (
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.payments.support import SECRET, sign

DATA_ID = "123456789"
REQUEST_ID = "req-1"


def command(
    secret: str | None = SECRET,
) -> tuple[ReceivePaymentNotification, RecordingEventPublisher]:
    events = RecordingEventPublisher()
    return (
        ReceivePaymentNotification(
            secret=secret, events=events, transactions=InMemoryTransactionRunner()
        ),
        events,
    )


async def test_a_signed_payment_notification_stores_one_event_and_answers_true() -> None:
    use_case, events = command()

    result = await use_case.execute(sign(DATA_ID, REQUEST_ID), REQUEST_ID, DATA_ID, "payment")

    assert result == Ok(True)
    assert [(e.name, e.payload()) for e in events.published] == [
        ("payments.notification.received", {"payment_id": DATA_ID})
    ]


@pytest.mark.parametrize("secret", [None, ""])
async def test_without_a_secret_it_is_unavailable_and_stores_nothing(secret: str | None) -> None:
    use_case, events = command(secret)

    with capture_logs() as logs:
        result = await use_case.execute(sign(DATA_ID, REQUEST_ID), REQUEST_ID, DATA_ID, "payment")

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert events.published == []
    assert [entry["event"] for entry in logs] == ["payments.webhook_not_configured"]


@pytest.mark.parametrize("header", [None, "", "ts=1,v1=00", "garbage"])
async def test_a_wrong_or_missing_signature_is_refused_and_stores_nothing(
    header: str | None,
) -> None:
    use_case, events = command()

    result = await use_case.execute(header, REQUEST_ID, DATA_ID, "payment")

    assert isinstance(result, Err)
    assert result.error == PaymentWebhookSignatureInvalid()
    assert events.published == []


async def test_a_signature_for_another_payment_id_is_refused() -> None:
    use_case, events = command()

    result = await use_case.execute(sign("999", REQUEST_ID), REQUEST_ID, DATA_ID, "payment")

    assert isinstance(result, Err)
    assert isinstance(result.error, PaymentWebhookSignatureInvalid)
    assert events.published == []


async def test_a_refused_signature_logs_the_request_id_and_never_the_header() -> None:
    use_case, _ = command()
    header = sign(DATA_ID, REQUEST_ID, secret="wrong")

    with capture_logs() as logs:
        await use_case.execute(header, REQUEST_ID, DATA_ID, "payment")

    assert logs == [
        {
            "event": "payments.webhook_signature_invalid",
            "x_request_id": REQUEST_ID,
            "log_level": "warning",
        }
    ]
    assert header not in repr(logs)


async def test_the_signature_is_checked_before_the_kind() -> None:
    use_case, events = command()

    result = await use_case.execute("bad", REQUEST_ID, DATA_ID, "merchant_order")

    assert isinstance(result, Err)
    assert isinstance(result.error, PaymentWebhookSignatureInvalid)
    assert events.published == []


@pytest.mark.parametrize("kind", ["merchant_order", "topic_merchant_order_wh", "plan", "", None])
async def test_a_signed_notification_of_another_kind_is_ignored(kind: str | None) -> None:
    use_case, events = command()

    with capture_logs() as logs:
        result = await use_case.execute(sign(DATA_ID, REQUEST_ID), REQUEST_ID, DATA_ID, kind)

    assert result == Ok(False)
    assert events.published == []
    assert [entry["event"] for entry in logs] == ["payments.webhook_ignored"]


@pytest.mark.parametrize("data_id", ["abc", "12a", "1.5", "-5", " 12", "", "9" * 21])
async def test_a_signed_payment_with_a_malformed_id_is_ignored(data_id: str) -> None:
    use_case, events = command()

    result = await use_case.execute(sign(data_id, REQUEST_ID), REQUEST_ID, data_id, "payment")

    assert result == Ok(False)
    assert events.published == []


async def test_a_signed_payment_without_an_id_is_ignored() -> None:
    use_case, events = command()

    result = await use_case.execute(sign(None, REQUEST_ID), REQUEST_ID, None, "payment")

    assert result == Ok(False)
    assert events.published == []


@pytest.mark.parametrize("data_id", ["1", "9" * 20])
async def test_an_id_of_one_to_twenty_digits_is_accepted(data_id: str) -> None:
    use_case, events = command()

    result = await use_case.execute(sign(data_id, REQUEST_ID), REQUEST_ID, data_id, "payment")

    assert result == Ok(True)
    assert [e.payload() for e in events.published] == [{"payment_id": data_id}]
    assert isinstance(events.published[0], PaymentNotificationReceived)


async def test_a_notification_without_a_request_id_is_accepted_when_signed_that_way() -> None:
    use_case, events = command()

    result = await use_case.execute(sign(DATA_ID, None), None, DATA_ID, "payment")

    assert result == Ok(True)
    assert len(events.published) == 1


async def test_the_same_notification_twice_stores_two_events_for_the_idempotent_handler() -> None:
    use_case, events = command()
    header = sign(DATA_ID, REQUEST_ID)

    await use_case.execute(header, REQUEST_ID, DATA_ID, "payment")
    await use_case.execute(header, REQUEST_ID, DATA_ID, "payment")

    assert [e.payload() for e in events.published] == [{"payment_id": DATA_ID}] * 2
