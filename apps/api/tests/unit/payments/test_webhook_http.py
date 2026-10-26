"""Plan 002 step 6, http: `POST /api/v1/payments/webhooks/mercadopago` (router.py): 200 for
accepted and ignored notifications, 401 `PAYMENTS_WEBHOOK_SIGNATURE_INVALID`, 503 without a
secret, the `data.id` query alias, the body never read, and the OpenAPI document."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.payments.application.commands.receive_notification import (
    ReceivePaymentNotification,
)
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import (
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from tests.support import TestActorResolver
from tests.unit.payments.support import SECRET, TS, sign

WEBHOOK = "/api/v1/payments/webhooks/mercadopago"
DATA_ID = "123456789"
REQUEST_ID = "req-http-1"


def build(events: RecordingEventPublisher, secret: str | None = SECRET):  # type: ignore[no-untyped-def]
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        ReceivePaymentNotification,
        ReceivePaymentNotification(
            secret=secret, events=events, transactions=InMemoryTransactionRunner()
        ),
    )
    return build_app(services, routers)


@pytest.fixture
def events() -> RecordingEventPublisher:
    return RecordingEventPublisher()


@pytest.fixture
async def client(events: RecordingEventPublisher) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=build(events)), base_url="http://t") as c:
        yield c


def headers(data_id: str | None = DATA_ID, request_id: str | None = REQUEST_ID) -> dict[str, str]:
    found = {"x-signature": sign(data_id, request_id)}
    if request_id:
        found["x-request-id"] = request_id
    return found


async def test_a_signed_payment_notification_answers_200_and_stores_one_event(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(f"{WEBHOOK}?data.id={DATA_ID}&type=payment", headers=headers())

    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert [(e.name, e.payload()) for e in events.published] == [
        ("payments.notification.received", {"payment_id": DATA_ID})
    ]


async def test_the_data_id_is_read_from_the_dotted_query_name(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    # The snake-case spelling is not the alias: it is not read, so the id is absent and the
    # notification is ignored (plan 002 step 7: ignored before the signature is looked at).
    response = await client.post(f"{WEBHOOK}?data_id={DATA_ID}&type=payment", headers=headers())

    assert response.status_code == 200
    assert events.published == []


async def test_the_body_is_not_read_and_cannot_change_the_payment(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(
        f"{WEBHOOK}?data.id={DATA_ID}&type=payment",
        headers=headers(),
        content=b"{not json at all",
    )

    assert response.status_code == 200
    assert [e.payload() for e in events.published] == [{"payment_id": DATA_ID}]


async def test_a_json_body_naming_another_payment_is_ignored(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(
        f"{WEBHOOK}?data.id={DATA_ID}&type=payment",
        headers=headers(),
        json={"type": "payment", "data": {"id": "999"}},
    )

    assert response.status_code == 200
    assert [e.payload() for e in events.published] == [{"payment_id": DATA_ID}]


@pytest.mark.xfail(
    strict=True,
    reason="GAP: payments-mercadopago/002 step 3 ('request-id only when present'): the platform's "
    "BodySizeLimitMiddleware injects a generated X-Request-ID, so the manifest never matches; "
    "plans/findings/payments-webhook-request-id-injected-by-platform.md",
)
async def test_a_notification_without_a_request_id_is_accepted_when_signed_that_way(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(
        f"{WEBHOOK}?data.id={DATA_ID}&type=payment", headers=headers(request_id=None)
    )

    assert response.status_code == 200
    assert len(events.published) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"x-signature": ""},
        {"x-signature": "ts=1,v1=deadbeef"},
        {"x-signature": f"ts={TS}"},
        {"x-signature": "garbage"},
        {"x-signature": sign(DATA_ID, REQUEST_ID, secret="other")},
    ],
)
async def test_a_wrong_or_missing_signature_is_401_and_stores_nothing(
    client: AsyncClient, events: RecordingEventPublisher, extra: dict[str, str]
) -> None:
    response = await client.post(
        f"{WEBHOOK}?data.id={DATA_ID}&type=payment",
        headers={"x-request-id": REQUEST_ID, **extra},
    )

    assert response.status_code == 401
    assert response.json() == {
        "code": "PAYMENTS_WEBHOOK_SIGNATURE_INVALID",
        "message": "Firma inválida",
    }
    assert events.published == []


async def test_a_signature_for_another_payment_id_is_401(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(
        f"{WEBHOOK}?data.id=999&type=payment", headers=headers(DATA_ID, REQUEST_ID)
    )

    assert response.status_code == 401
    assert events.published == []


@pytest.mark.parametrize("kind", ["merchant_order", "topic_merchant_order_wh", "plan"])
async def test_a_signed_notification_of_another_type_answers_200_and_stores_nothing(
    client: AsyncClient, events: RecordingEventPublisher, kind: str
) -> None:
    response = await client.post(f"{WEBHOOK}?data.id={DATA_ID}&type={kind}", headers=headers())

    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert events.published == []


async def test_a_signed_notification_without_a_type_answers_200_and_stores_nothing(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(f"{WEBHOOK}?data.id={DATA_ID}", headers=headers())

    assert response.status_code == 200
    assert events.published == []


async def test_a_signed_payment_with_a_malformed_id_answers_200_and_stores_nothing(
    client: AsyncClient, events: RecordingEventPublisher
) -> None:
    response = await client.post(
        f"{WEBHOOK}?data.id=abc&type=payment", headers=headers("abc", REQUEST_ID)
    )

    assert response.status_code == 200
    assert events.published == []


@pytest.mark.parametrize("secret", [None, ""])
async def test_without_a_webhook_secret_it_is_503_with_the_gateway_code(
    events: RecordingEventPublisher, secret: str | None
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=build(events, secret)), base_url="http://t"
    ) as c:
        response = await c.post(f"{WEBHOOK}?data.id={DATA_ID}&type=payment", headers=headers())

    assert response.status_code == 503
    assert response.json()["code"] == "PAYMENTS_GATEWAY_UNAVAILABLE"
    assert events.published == []


async def test_the_route_is_public_and_needs_no_session_cookie(client: AsyncClient) -> None:
    response = await client.post(f"{WEBHOOK}?data.id={DATA_ID}&type=payment", headers=headers())

    assert response.status_code == 200


async def test_the_route_does_not_answer_get(client: AsyncClient) -> None:
    response = await client.get(f"{WEBHOOK}?data.id={DATA_ID}&type=payment", headers=headers())

    assert response.status_code == 405


async def test_the_response_has_only_the_received_flag(client: AsyncClient) -> None:
    response = await client.post(f"{WEBHOOK}?data.id={DATA_ID}&type=payment", headers=headers())

    assert set(response.json()) == {"received"}
    assert SECRET not in response.text


async def test_openapi_declares_the_query_the_headers_the_statuses_and_no_body(
    events: RecordingEventPublisher,
) -> None:
    schema = build(events).openapi()

    operation = schema["paths"][WEBHOOK]["post"]
    assert set(schema["paths"][WEBHOOK]) == {"post"}
    assert "requestBody" not in operation
    parameters = {(p["in"], p["name"]) for p in operation["parameters"]}
    assert parameters == {
        ("query", "data.id"),
        ("query", "type"),
        ("header", "x-signature"),
        ("header", "x-request-id"),
    }
    assert all(not p.get("required") for p in operation["parameters"])
    assert {"200", "401", "503"} <= set(operation["responses"])
    ack = schema["components"]["schemas"]["WebhookAck"]
    assert ack["properties"]["received"]["default"] is True
    assert operation["tags"] == ["payments"]
