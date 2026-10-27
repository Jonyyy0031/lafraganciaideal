"""Plan 002 steps 1 and 5: `MercadoPagoGateway.get_payment` against `httpx.MockTransport` (request,
mapping, 404, every failure mode, no token in logs) and the `auto_return` rule of `create_checkout`
(only with an https success URL, decision 8)."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.ports import GatewayPayment
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import TIMEOUT_SECONDS
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.payments.support import make_request
from tests.unit.payments.test_mercadopago_gateway import (
    ACCESS_TOKEN,
    API,
    GOOD,
    Recorder,
    answering,
    body_of,
    gateway_for,
    raising,
)

PAID = {
    "id": 1234567890,
    "status": "approved",
    "status_detail": "accredited",
    "external_reference": "00000000-0000-0000-0000-000000000384",
    "transaction_amount": 2680.0,
    "currency_id": "MXN",
    "date_approved": "2026-10-24T12:30:15.000-06:00",
    "payment_type_id": "credit_card",
    "live_mode": False,
}


# --- the request -------------------------------------------------------------------------------


async def test_it_gets_the_payment_with_a_bearer_token_and_no_body() -> None:
    recorder = answering(200, PAID)

    await gateway_for(recorder).get_payment("1234567890")

    sent = recorder.sent
    assert sent.method == "GET"
    assert str(sent.url) == f"{API}/v1/payments/1234567890"
    assert sent.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert sent.content == b""
    assert sent.extensions["timeout"] == {
        "connect": TIMEOUT_SECONDS,
        "read": TIMEOUT_SECONDS,
        "write": TIMEOUT_SECONDS,
        "pool": TIMEOUT_SECONDS,
    }


async def test_a_trailing_slash_in_the_api_url_does_not_double_the_slash() -> None:
    recorder = answering(200, PAID)

    await gateway_for(recorder, api_url=f"{API}/").get_payment("1")

    assert str(recorder.sent.url) == f"{API}/v1/payments/1"


async def test_the_token_travels_only_in_the_authorization_header() -> None:
    recorder = answering(200, PAID)

    await gateway_for(recorder).get_payment("1")

    sent = recorder.sent
    assert ACCESS_TOKEN not in str(sent.url)
    assert [n for n, v in sent.headers.items() if ACCESS_TOKEN in v] == ["authorization"]


async def test_each_call_opens_its_own_client_without_the_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any) -> httpx.AsyncClient:
        opened.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)

    await gateway_for(answering(200, PAID)).get_payment("1")

    assert [(k["trust_env"], k["timeout"]) for k in opened] == [(False, TIMEOUT_SECONDS)]


# --- the answer --------------------------------------------------------------------------------


async def test_a_paid_payment_is_mapped_field_by_field() -> None:
    result = await gateway_for(answering(200, PAID)).get_payment("1234567890")

    assert result == Ok(
        GatewayPayment(
            payment_id="1234567890",
            status="approved",
            status_detail="accredited",
            external_reference="00000000-0000-0000-0000-000000000384",
            amount_cents=268_000,
            currency="MXN",
            payment_type="credit_card",
            approved_at=datetime(2026, 10, 24, 12, 30, 15, tzinfo=timezone(timedelta(hours=-6))),
        )
    )


async def test_the_payment_id_is_the_numeric_id_of_the_answer_as_text() -> None:
    result = await gateway_for(answering(200, {**PAID, "id": 42})).get_payment("999")

    assert isinstance(result, Ok)
    assert result.value is not None
    assert result.value.payment_id == "42"


async def test_a_string_id_is_accepted() -> None:
    result = await gateway_for(answering(200, {**PAID, "id": "42"})).get_payment("42")

    assert isinstance(result, Ok)
    assert result.value is not None
    assert result.value.payment_id == "42"


async def test_a_pending_payment_without_optional_fields_maps_them_to_none() -> None:
    body = {"id": 7, "status": "pending", "transaction_amount": 10, "currency_id": "MXN"}

    result = await gateway_for(answering(200, body)).get_payment("7")

    assert result == Ok(
        GatewayPayment(
            payment_id="7",
            status="pending",
            status_detail=None,
            external_reference=None,
            amount_cents=1000,
            currency="MXN",
            payment_type=None,
            approved_at=None,
        )
    )


async def test_null_optional_fields_are_none() -> None:
    body = {
        **PAID,
        "status_detail": None,
        "external_reference": None,
        "payment_type_id": None,
        "date_approved": None,
    }

    result = await gateway_for(answering(200, body)).get_payment("1")

    assert isinstance(result, Ok)
    found = result.value
    assert found is not None
    assert (found.status_detail, found.external_reference) == (None, None)
    assert (found.payment_type, found.approved_at) == (None, None)


@pytest.mark.parametrize(
    ("pesos", "cents"),
    [(2680, 268_000), (2680.0, 268_000), (10.05, 1005), (0.1, 10), (1234.56, 123_456), (0.01, 1)],
)
async def test_the_amount_in_pesos_becomes_whole_cents(pesos: float, cents: int) -> None:
    result = await gateway_for(answering(200, {**PAID, "transaction_amount": pesos})).get_payment(
        "1"
    )

    assert isinstance(result, Ok)
    assert result.value is not None
    assert result.value.amount_cents == cents


async def test_the_currency_is_passed_through_even_when_it_is_not_mxn() -> None:
    result = await gateway_for(answering(200, {**PAID, "currency_id": "USD"})).get_payment("1")

    assert isinstance(result, Ok)
    assert result.value is not None
    assert result.value.currency == "USD"


async def test_mercado_pago_not_knowing_the_payment_is_ok_none() -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(404, {"message": "Payment not found"})).get_payment(
            "1"
        )

    assert result == Ok(None)
    assert logs == []


# --- failures ----------------------------------------------------------------------------------

BAD_BODIES: dict[str, Any] = {
    "missing_id": {k: v for k, v in PAID.items() if k != "id"},
    "missing_status": {k: v for k, v in PAID.items() if k != "status"},
    "missing_amount": {k: v for k, v in PAID.items() if k != "transaction_amount"},
    "missing_currency": {k: v for k, v in PAID.items() if k != "currency_id"},
    "id_a_bool": {**PAID, "id": True},
    "id_a_float": {**PAID, "id": 1.5},
    "id_null": {**PAID, "id": None},
    "amount_a_string": {**PAID, "transaction_amount": "2680.00"},
    "amount_a_bool": {**PAID, "transaction_amount": True},
    "amount_null": {**PAID, "transaction_amount": None},
    "status_empty": {**PAID, "status": ""},
    "status_not_a_string": {**PAID, "status": 1},
    "currency_not_a_string": {**PAID, "currency_id": 484},
    "reference_not_a_string": {**PAID, "external_reference": 5},
    "payment_type_not_a_string": {**PAID, "payment_type_id": ["credit_card"]},
    "status_detail_not_a_string": {**PAID, "status_detail": {}},
    "date_not_a_string": {**PAID, "date_approved": 1700000000},
    "date_not_iso": {**PAID, "date_approved": "yesterday"},
    "json_list": [PAID],
    "json_null": None,
}


@pytest.mark.parametrize("name", BAD_BODIES)
async def test_a_body_the_gateway_cannot_trust_is_unavailable_and_logged_as_bad_body(
    name: str,
) -> None:
    gateway = gateway_for(answering(200, BAD_BODIES[name]))

    with capture_logs() as logs:
        result = await gateway.get_payment("1")

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert logs == [
        {
            "event": "payments.gateway_error",
            "status": 200,
            "error": "bad_body",
            "log_level": "error",
        }
    ]


NON_2XX = [400, 401, 403, 429, 500, 502, 503]


@pytest.mark.parametrize("status", NON_2XX)
async def test_a_non_2xx_answer_is_unavailable_and_logs_the_status_only(status: int) -> None:
    gateway = gateway_for(answering(status, {"message": "secret internal detail"}))

    with capture_logs() as logs:
        result = await gateway.get_payment("1")

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert logs == [{"event": "payments.gateway_error", "status": status, "log_level": "error"}]


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.ReadTimeout("x")]
)
async def test_a_transport_error_is_unavailable_and_logs_its_class(error: Exception) -> None:
    gateway = gateway_for(raising(error))

    with capture_logs() as logs:
        result = await gateway.get_payment("1")

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert logs == [
        {
            "event": "payments.gateway_error",
            "status": None,
            "error": type(error).__name__,
            "log_level": "error",
        }
    ]
    assert ACCESS_TOKEN not in repr(logs)


async def test_an_undecodable_body_is_unavailable() -> None:
    recorder = Recorder(lambda _: httpx.Response(200, content=b"<html>no</html>"))

    result = await gateway_for(recorder).get_payment("1")

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()


async def test_a_redirect_is_unavailable() -> None:
    recorder = Recorder(lambda _: httpx.Response(302, headers={"Location": "/x"}))

    result = await gateway_for(recorder).get_payment("1")

    assert isinstance(result, Err)


@pytest.mark.parametrize("token", [None, ""])
async def test_without_an_access_token_it_sends_nothing_and_logs_once(token: str | None) -> None:
    recorder = answering(200, PAID)

    with capture_logs() as logs:
        result = await gateway_for(recorder, token=token).get_payment("1")

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert recorder.requests == []
    assert logs == [{"event": "payments.gateway_not_configured", "log_level": "error"}]


async def test_no_log_of_a_success_or_a_failure_carries_the_token() -> None:
    with capture_logs() as logs:
        await gateway_for(answering(200, PAID)).get_payment("1")
        await gateway_for(answering(500, {})).get_payment("1")
        await gateway_for(answering(200, {"id": 1})).get_payment("1")

    assert ACCESS_TOKEN not in repr(logs)


# --- auto_return (step 1, decision 8) ----------------------------------------------------------


@pytest.mark.parametrize(
    "back_url",
    [
        "http://localhost:4200/pedido/pago",
        "http://127.0.0.1:4200/pedido/pago",
        "http://shop.example/pedido/pago",
        "HTTP://localhost:4200/pedido/pago",
        "ftp://shop.example/pedido/pago",
        "/pedido/pago",
        "",
    ],
)
async def test_without_an_https_back_url_there_is_no_auto_return(back_url: str) -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request(back_url=back_url))

    body = body_of(recorder.sent)
    assert "auto_return" not in body
    assert set(body["back_urls"]) == {"success", "pending", "failure"}
    assert set(body["back_urls"].values()) == {back_url}


@pytest.mark.parametrize(
    "back_url", ["https://shop.test/pedido/pago", "https://localhost:4200/pedido/pago"]
)
async def test_with_an_https_back_url_auto_return_is_approved(back_url: str) -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request(back_url=back_url))

    body = body_of(recorder.sent)
    assert body["auto_return"] == "approved"
    assert set(body["back_urls"].values()) == {back_url}


async def test_the_rest_of_the_body_is_the_same_with_or_without_auto_return() -> None:
    http = answering()
    https = answering()

    await gateway_for(http).create_checkout(make_request(back_url="http://x.test/b"))
    await gateway_for(https).create_checkout(make_request(back_url="https://x.test/b"))

    without = body_of(http.sent)
    with_ = body_of(https.sent)
    for body in (without, with_):
        body.pop("auto_return", None)
        body.pop("back_urls")
    assert without == with_


async def test_the_sandbox_link_still_comes_back_when_auto_return_is_left_out() -> None:
    result = await gateway_for(answering(), sandbox=True).create_checkout(
        make_request(back_url="http://localhost:4200/pedido/pago")
    )

    assert isinstance(result, Ok)
    assert result.value.redirect_url == GOOD["sandbox_init_point"]
