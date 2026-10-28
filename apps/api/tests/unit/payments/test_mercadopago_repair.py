"""Plan 003 step 8 (review repair), the Mercado Pago adapter: L2 (a ticket page longer than the
column is dropped and logged), L3 and repair deviation 5 (which statuses of `close_checkout` and
`cancel_payment` are permanent `Ok(False)` and which are `Err`), and the request shape of
`cancel_payment`."""

from datetime import UTC, datetime

import httpx
import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import (
    PERMANENT_REFUSALS,
    TICKET_URL_MAX_LENGTH,
    TIMEOUT_SECONDS,
)
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.payments.test_mercadopago_gateway import (
    ACCESS_TOKEN,
    API,
    Recorder,
    answering,
    body_of,
    gateway_for,
    raising,
)
from tests.unit.payments.test_mercadopago_tickets import OXXO

AT = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
PERMANENT = [400, 404, 409, 422]
RETRIED = [401, 403, 408, 429, 500, 502, 503]


def test_the_permanent_statuses_and_the_url_limit_are_the_planned_ones() -> None:
    assert PERMANENT_REFUSALS == frozenset({400, 404, 409, 422})
    assert TICKET_URL_MAX_LENGTH == 500


# --- L2: the ticket page ------------------------------------------------------------------------


async def test_a_page_of_exactly_500_characters_is_kept() -> None:
    url = "https://mp.test/" + "x" * (500 - len("https://mp.test/"))
    body = {**OXXO, "transaction_details": {"external_resource_url": url}}

    result = await gateway_for(answering(200, body)).get_payment("777")

    assert isinstance(result, Ok)
    assert result.value is not None
    assert result.value.ticket_url == url


async def test_a_page_over_500_characters_is_dropped_and_logged_without_failing() -> None:
    url = "https://mp.test/" + "x" * 600
    body = {**OXXO, "transaction_details": {"external_resource_url": url}}

    with capture_logs() as logs:
        result = await gateway_for(answering(200, body)).get_payment("777")

    assert isinstance(result, Ok)
    payment = result.value
    assert payment is not None
    assert payment.ticket_url is None
    assert (payment.status, payment.payment_method) == ("pending", "oxxo")
    [log] = [e for e in logs if e["event"] == "payments.ticket_url_too_long"]
    assert (log["payment_id"], log["length"]) == ("777", len(url))
    assert url not in str(logs)


# --- L3 / deviation 5: close_checkout ----------------------------------------------------------


@pytest.mark.parametrize("status", [400, 409, 422])
async def test_close_with_a_permanent_refusal_is_ok_false_and_logged_as_refused(
    status: int,
) -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(status, {"message": "no"})).close_checkout("p", AT)

    assert result == Ok(False)
    [log] = [e for e in logs if e["event"] == "payments.checkout_close_refused"]
    assert log["status"] == status
    assert log["log_level"] == "warning"


async def test_close_of_an_unknown_preference_stays_silent() -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(404, {})).close_checkout("p", AT)

    assert result == Ok(False)
    assert logs == []


@pytest.mark.parametrize("status", RETRIED)
async def test_close_with_a_retryable_status_is_err_and_never_logged_as_refused(
    status: int,
) -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(status, {})).close_checkout("p", AT)

    assert result == Err(PaymentGatewayUnavailable())
    assert [e["event"] for e in logs] == ["payments.gateway_error"]


# --- cancel_payment ----------------------------------------------------------------------------


async def test_cancel_puts_the_cancelled_status_to_the_payment_with_a_bearer_token() -> None:
    recorder = answering(200, {"id": 777, "status": "cancelled"})

    result = await gateway_for(recorder).cancel_payment("777")

    assert result == Ok(True)
    sent = recorder.sent
    assert sent.method == "PUT"
    assert str(sent.url) == f"{API}/v1/payments/777"
    assert sent.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert sent.headers["Content-Type"] == "application/json"
    assert body_of(sent) == {"status": "cancelled"}
    assert sent.extensions["timeout"] == {
        "connect": TIMEOUT_SECONDS,
        "read": TIMEOUT_SECONDS,
        "write": TIMEOUT_SECONDS,
        "pool": TIMEOUT_SECONDS,
    }


async def test_cancel_accepts_any_2xx() -> None:
    assert await gateway_for(answering(204, None)).cancel_payment("777") == Ok(True)


async def test_cancel_uses_the_configured_api_url_without_doubling_the_slash() -> None:
    recorder = answering(200, {})

    await gateway_for(recorder, api_url=f"{API}/").cancel_payment("9")

    assert str(recorder.sent.url) == f"{API}/v1/payments/9"


@pytest.mark.parametrize("status", PERMANENT)
async def test_cancel_with_a_permanent_refusal_is_ok_false_and_logged_with_the_payment(
    status: int,
) -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(status, {"message": "no"})).cancel_payment("777")

    assert result == Ok(False)
    [log] = logs
    assert log["event"] == "payments.ticket_cancel_refused"
    assert (log["payment_id"], log["status"], log["log_level"]) == ("777", status, "warning")


@pytest.mark.parametrize("status", RETRIED)
async def test_cancel_with_a_retryable_status_is_err_and_logs_the_status_only(
    status: int,
) -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(status, {"secret": ACCESS_TOKEN})).cancel_payment(
            "777"
        )

    assert result == Err(PaymentGatewayUnavailable())
    [log] = logs
    assert (log["event"], log["status"]) == ("payments.gateway_error", status)
    assert ACCESS_TOKEN not in str(logs)


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
async def test_cancel_with_a_transport_error_is_err_and_logs_its_class(error: Exception) -> None:
    with capture_logs() as logs:
        result = await gateway_for(raising(error)).cancel_payment("777")

    assert result == Err(PaymentGatewayUnavailable())
    [log] = logs
    assert (log["event"], log["status"], log["error"]) == (
        "payments.gateway_error",
        None,
        type(error).__name__,
    )


@pytest.mark.parametrize("token", [None, ""])
async def test_cancel_without_an_access_token_sends_nothing_and_logs_once(
    token: str | None,
) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={}))

    with capture_logs() as logs:
        result = await gateway_for(recorder, token=token).cancel_payment("777")

    assert result == Err(PaymentGatewayUnavailable())
    assert recorder.requests == []
    assert [e["event"] for e in logs] == ["payments.gateway_not_configured"]
