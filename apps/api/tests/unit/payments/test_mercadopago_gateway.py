"""`MercadoPagoGateway` (mercadopago_gateway.py) against `httpx.MockTransport`: the exact request
body and headers, the sandbox / production URL choice, and every failure mode (nothing stored,
the access token never logged). No network: the transport answers in process."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.ports import CheckoutItem, CheckoutLink
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import (
    TIMEOUT_SECONDS,
    MercadoPagoGateway,
)
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.payments.support import BACK_URL, ORDER_ID, TOKEN, World, make_payable, make_request

API = "https://mp.test"
ACCESS_TOKEN = "TEST-1234567890-secret-access-token"
INIT = "https://www.mercadopago.com.mx/checkout/v1/redirect?pref_id=123"
SANDBOX_INIT = "https://sandbox.mercadopago.com.mx/checkout/v1/redirect?pref_id=123"
GOOD = {"id": "123-abc", "init_point": INIT, "sandbox_init_point": SANDBOX_INIT}


class Recorder:
    """A `MockTransport` handler that records the requests and answers `respond(request)`."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def sent(self) -> httpx.Request:
        [request] = self.requests
        return request


def answering(status: int = 201, body: Any = GOOD) -> Recorder:
    return Recorder(lambda _: httpx.Response(status, json=body))


def gateway_for(
    recorder: Recorder,
    *,
    sandbox: bool = True,
    token: str | None = ACCESS_TOKEN,
    api_url: str = API,
) -> MercadoPagoGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return MercadoPagoGateway(api_url=api_url, access_token=token, sandbox=sandbox, client=client)


def body_of(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


# --- the request -------------------------------------------------------------------------------


async def test_it_posts_the_exact_preference_body() -> None:
    recorder = answering()
    expires = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)

    result = await gateway_for(recorder).create_checkout(make_request(expires_at=expires))

    assert isinstance(result, Ok)
    assert body_of(recorder.sent) == {
        "items": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "title": "Brand Perfume 1",
                "quantity": 2,
                "unit_price": 2500.0,
                "currency_id": "MXN",
            },
            {
                "id": "shipping",
                "title": "Envío",
                "quantity": 1,
                "unit_price": 180.0,
                "currency_id": "MXN",
            },
        ],
        "payer": {"name": "Ana López", "email": "ana@example.com"},
        "external_reference": str(ORDER_ID),
        "back_urls": {"success": BACK_URL, "pending": BACK_URL, "failure": BACK_URL},
        "auto_return": "approved",
        "payment_methods": {
            "excluded_payment_types": [{"id": "ticket"}, {"id": "atm"}, {"id": "bank_transfer"}],
            "installments": 1,
        },
        "expires": True,
        "expiration_date_to": "2026-10-22T18:30:00+00:00",
    }


async def test_it_sends_no_notification_url_and_no_other_field() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request())

    assert set(body_of(recorder.sent)) == {
        "items",
        "payer",
        "external_reference",
        "back_urls",
        "auto_return",
        "payment_methods",
        "expires",
        "expiration_date_to",
    }


async def test_it_posts_to_the_preferences_endpoint_with_a_bearer_token_and_a_json_body() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request())

    sent = recorder.sent
    assert sent.method == "POST"
    assert str(sent.url) == f"{API}/checkout/preferences"
    assert sent.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert sent.headers["Content-Type"] == "application/json"


async def test_a_trailing_slash_in_the_api_url_does_not_double_the_slash() -> None:
    recorder = answering()

    await gateway_for(recorder, api_url=f"{API}/").create_checkout(make_request())

    assert str(recorder.sent.url) == f"{API}/checkout/preferences"


async def test_the_access_token_travels_only_in_the_authorization_header() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request())

    sent = recorder.sent
    assert ACCESS_TOKEN not in sent.content.decode()
    assert ACCESS_TOKEN not in str(sent.url)
    carriers = [name for name, value in sent.headers.items() if ACCESS_TOKEN in value]
    assert carriers == ["authorization"]


async def test_the_request_has_a_ten_second_timeout() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request())

    assert TIMEOUT_SECONDS == 10.0
    assert recorder.sent.extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }


@pytest.mark.parametrize(
    ("cents", "pesos"),
    [
        (1, 0.01),
        (5, 0.05),
        (10, 0.1),
        (199, 1.99),
        (1005, 10.05),
        (99_900, 999.0),
        (250_000, 2500.0),
    ],
)
async def test_the_unit_price_is_the_cents_in_pesos(cents: int, pesos: float) -> None:
    recorder = answering()
    items = [CheckoutItem("x", "Item", 1, cents)]

    await gateway_for(recorder).create_checkout(make_request(items=items))

    [item] = body_of(recorder.sent)["items"]
    assert item["unit_price"] == pesos
    assert isinstance(item["unit_price"], float)


async def test_the_unit_price_is_a_json_number_with_two_decimals_not_a_string_or_cents() -> None:
    recorder = answering()
    items = [CheckoutItem("x", "Item", 3, 123_456)]

    await gateway_for(recorder).create_checkout(make_request(items=items))

    raw = recorder.sent.content.decode()
    assert '"unit_price":1234.56' in raw.replace(" ", "")
    assert body_of(recorder.sent)["items"][0]["quantity"] == 3


async def test_the_items_keep_their_amounts_summing_to_the_order_total_in_pesos() -> None:
    recorder = answering()
    world = World()
    world.put(make_payable())

    await world.start_checkout(gateway_for(recorder)).execute(TOKEN)

    items = body_of(recorder.sent)["items"]
    order = make_payable()
    assert sum(round(i["quantity"] * i["unit_price"] * 100) for i in items) == order.total_cents
    assert {i["currency_id"] for i in items} == {"MXN"}
    assert items[-1]["id"] == "shipping"


async def test_the_expiration_keeps_the_offset_of_the_datetime() -> None:
    recorder = answering()
    mexico = timezone(timedelta(hours=-6))
    expires = datetime(2026, 10, 22, 12, 30, tzinfo=mexico)

    await gateway_for(recorder).create_checkout(make_request(expires_at=expires))

    body = body_of(recorder.sent)
    assert body["expiration_date_to"] == "2026-10-22T12:30:00-06:00"
    assert body["expires"] is True


async def test_the_external_reference_is_the_order_id() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request())

    assert body_of(recorder.sent)["external_reference"] == str(ORDER_ID)


async def test_the_three_back_urls_are_the_given_back_url() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request(back_url="https://x.test/back"))

    body = body_of(recorder.sent)
    assert set(body["back_urls"].values()) == {"https://x.test/back"}
    assert set(body["back_urls"]) == {"success", "pending", "failure"}


async def test_only_cards_in_one_installment() -> None:
    recorder = answering()

    await gateway_for(recorder).create_checkout(make_request())

    methods = body_of(recorder.sent)["payment_methods"]
    assert [kind["id"] for kind in methods["excluded_payment_types"]] == [
        "ticket",
        "atm",
        "bank_transfer",
    ]
    assert methods["installments"] == 1


# --- the answer and the sandbox / production choice -------------------------------------------


async def test_in_sandbox_it_answers_the_sandbox_init_point() -> None:
    result = await gateway_for(answering(), sandbox=True).create_checkout(make_request())

    assert result == Ok(CheckoutLink(preference_id="123-abc", redirect_url=SANDBOX_INIT))


async def test_in_production_it_answers_the_init_point() -> None:
    result = await gateway_for(answering(), sandbox=False).create_checkout(make_request())

    assert result == Ok(CheckoutLink(preference_id="123-abc", redirect_url=INIT))


async def test_a_200_answer_is_a_success_too() -> None:
    result = await gateway_for(answering(200)).create_checkout(make_request())

    assert isinstance(result, Ok)


async def test_the_api_url_is_the_configured_one() -> None:
    recorder = answering()

    await gateway_for(recorder, api_url="http://127.0.0.1:9999").create_checkout(make_request())

    assert str(recorder.sent.url) == "http://127.0.0.1:9999/checkout/preferences"


# --- failures ----------------------------------------------------------------------------------


def raising(error: Exception) -> Recorder:
    def respond(_: httpx.Request) -> httpx.Response:
        raise error

    return Recorder(respond)


def bad_json(_: httpx.Request) -> httpx.Response:
    return httpx.Response(201, content=b"<html>not json</html>")


FAILURES: dict[str, Callable[[], Recorder]] = {
    "connect_error": lambda: raising(httpx.ConnectError("refused")),
    "connect_timeout": lambda: raising(httpx.ConnectTimeout("slow")),
    "read_timeout": lambda: raising(httpx.ReadTimeout("slow")),
    "bad_request": lambda: answering(400, {"message": "invalid items"}),
    "unauthorized": lambda: answering(401, {"message": "invalid access token"}),
    "forbidden": lambda: answering(403, {}),
    "rate_limited": lambda: answering(429, {}),
    "server_error": lambda: answering(500, {"message": "boom"}),
    "bad_gateway": lambda: answering(502, GOOD),
    "redirect": lambda: Recorder(lambda _: httpx.Response(302, headers={"Location": "/x"})),
    "no_content": lambda: Recorder(lambda _: httpx.Response(204)),
    "not_json": lambda: Recorder(bad_json),
    "json_list": lambda: answering(201, [GOOD]),
    "json_null": lambda: answering(201, None),
    "missing_id": lambda: answering(201, {"init_point": INIT, "sandbox_init_point": SANDBOX_INIT}),
    "missing_urls": lambda: answering(201, {"id": "123-abc"}),
    "missing_sandbox_point": lambda: answering(201, {"id": "123-abc", "init_point": INIT}),
    "id_not_a_string": lambda: answering(201, {**GOOD, "id": 123}),
    "empty_id": lambda: answering(201, {**GOOD, "id": ""}),
    "url_not_a_string": lambda: answering(201, {**GOOD, "sandbox_init_point": None}),
    "empty_url": lambda: answering(201, {**GOOD, "sandbox_init_point": ""}),
}


@pytest.mark.parametrize("name", FAILURES)
async def test_every_failure_mode_answers_gateway_unavailable_without_leaking_the_token(
    name: str,
) -> None:
    gateway = gateway_for(FAILURES[name]())

    with capture_logs() as logs:
        result = await gateway.create_checkout(make_request())

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert [entry["event"] for entry in logs] == ["payments.gateway_error"]
    assert ACCESS_TOKEN not in repr(logs)
    assert logs[0]["log_level"] == "error"


async def test_a_non_2xx_answer_logs_its_status_and_nothing_of_the_body() -> None:
    gateway = gateway_for(answering(500, {"message": "secret internal detail"}))

    with capture_logs() as logs:
        await gateway.create_checkout(make_request())

    assert logs == [{"event": "payments.gateway_error", "status": 500, "log_level": "error"}]


async def test_a_transport_error_logs_its_class_and_no_status() -> None:
    gateway = gateway_for(raising(httpx.ReadTimeout("slow")))

    with capture_logs() as logs:
        await gateway.create_checkout(make_request())

    assert logs == [
        {
            "event": "payments.gateway_error",
            "status": None,
            "error": "ReadTimeout",
            "log_level": "error",
        }
    ]


async def test_a_body_missing_the_fields_logs_its_status_and_bad_body() -> None:
    gateway = gateway_for(answering(201, {"id": "x"}))

    with capture_logs() as logs:
        await gateway.create_checkout(make_request())

    assert logs == [
        {
            "event": "payments.gateway_error",
            "status": 201,
            "error": "bad_body",
            "log_level": "error",
        }
    ]


@pytest.mark.parametrize("token", [None, ""])
async def test_without_an_access_token_it_sends_nothing_and_logs_once(token: str | None) -> None:
    recorder = answering()

    with capture_logs() as logs:
        result = await gateway_for(recorder, token=token).create_checkout(make_request())

    assert isinstance(result, Err)
    assert result.error == PaymentGatewayUnavailable()
    assert recorder.requests == []
    assert logs == [{"event": "payments.gateway_not_configured", "log_level": "error"}]


async def test_it_logs_once_per_call_when_not_configured() -> None:
    gateway = gateway_for(answering(), token=None)

    with capture_logs() as logs:
        await gateway.create_checkout(make_request())
        await gateway.create_checkout(make_request())

    assert [entry["event"] for entry in logs] == ["payments.gateway_not_configured"] * 2


@pytest.mark.parametrize("name", ["connect_error", "read_timeout", "server_error", "not_json"])
async def test_a_failed_gateway_makes_the_use_case_store_nothing(name: str) -> None:
    world = World()
    world.put(make_payable())

    result = await world.start_checkout(gateway_for(FAILURES[name]())).execute(TOKEN)

    assert isinstance(result, Err)
    assert isinstance(result.error, PaymentGatewayUnavailable)
    assert world.checkouts.by_order == {}


async def test_an_unconfigured_gateway_makes_the_use_case_store_nothing() -> None:
    world = World()
    recorder = answering()
    world.put(make_payable())

    result = await world.start_checkout(gateway_for(recorder, token=None)).execute(TOKEN)

    assert isinstance(result, Err)
    assert isinstance(result.error, PaymentGatewayUnavailable)
    assert recorder.requests == []
    assert world.checkouts.by_order == {}


async def test_a_successful_gateway_makes_the_use_case_store_the_preference() -> None:
    world = World()
    world.put(make_payable())

    result = await world.start_checkout(gateway_for(answering(), sandbox=True)).execute(TOKEN)

    assert result == Ok(SANDBOX_INIT)
    stored = world.checkouts.by_order[ORDER_ID]
    assert (stored.preference_id, stored.redirect_url) == ("123-abc", SANDBOX_INIT)
