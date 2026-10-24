"""`POST /api/v1/payments/checkout` (router.py): statuses, error codes, the token only in the
body, the 503 category mapping and the OpenAPI document."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.modules.payments.infrastructure.in_memory import FakeGateway
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import TestActorResolver
from tests.unit.payments.support import NOW, ORDER_ID, TOKEN, World, make_checkout, make_payable

CHECKOUT = "/api/v1/payments/checkout"


def build(world: World):  # type: ignore[no-untyped-def]
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(StartCheckout, world.start_checkout())
    return build_app(services, routers)


@pytest.fixture
def world() -> World:
    world = World()
    world.put(make_payable())
    return world


@pytest.fixture
async def client(world: World) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=build(world)), base_url="http://t") as c:
        yield c


async def test_a_payable_order_answers_200_with_the_redirect_url(client: AsyncClient) -> None:
    response = await client.post(CHECKOUT, json={"access_token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {"redirect_url": "https://pay.test/checkout/pref-1"}


async def test_a_second_request_answers_the_same_url(client: AsyncClient, world: World) -> None:
    first = await client.post(CHECKOUT, json={"access_token": TOKEN})
    second = await client.post(CHECKOUT, json={"access_token": TOKEN})

    assert second.json() == first.json()
    assert len(world.gateway.requests) == 1


async def test_an_unknown_token_is_404(client: AsyncClient) -> None:
    response = await client.post(CHECKOUT, json={"access_token": "nope"})

    assert response.status_code == 404
    assert response.json()["code"] == "PAYMENTS_ORDER_NOT_FOUND"


@pytest.mark.parametrize("status", ["paid", "preparing", "shipped", "delivered", "cancelled"])
async def test_an_order_that_is_not_pending_payment_is_409_with_its_status(
    status: str,
) -> None:
    world = World()
    world.put(make_payable(status=status))
    async with AsyncClient(transport=ASGITransport(app=build(world)), base_url="http://t") as c:
        response = await c.post(CHECKOUT, json={"access_token": TOKEN})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "PAYMENTS_ORDER_NOT_PAYABLE"
    assert body["message"] == "Este pedido ya no se puede pagar"
    assert body["details"] == {"status": status}


async def test_an_order_past_its_deadline_is_409() -> None:
    world = World()
    world.put(make_payable(due=-timedelta(seconds=1)))
    async with AsyncClient(transport=ASGITransport(app=build(world)), base_url="http://t") as c:
        response = await c.post(CHECKOUT, json={"access_token": TOKEN})

    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENTS_ORDER_NOT_PAYABLE"


async def test_a_gateway_failure_is_503_with_its_code_and_stores_nothing() -> None:
    world = World(gateway=FakeGateway(fail=True))
    world.put(make_payable())
    async with AsyncClient(transport=ASGITransport(app=build(world)), base_url="http://t") as c:
        response = await c.post(CHECKOUT, json={"access_token": TOKEN})

    assert response.status_code == 503
    assert response.json() == {
        "code": "PAYMENTS_GATEWAY_UNAVAILABLE",
        "message": "No pudimos conectar con Mercado Pago; intenta de nuevo",
    }
    assert world.checkouts.by_order == {}


async def test_a_stored_link_inside_the_margin_is_replaced_over_http(world: World) -> None:
    world.checkouts.by_order[ORDER_ID] = make_checkout(NOW + timedelta(minutes=1))
    async with AsyncClient(transport=ASGITransport(app=build(world)), base_url="http://t") as c:
        response = await c.post(CHECKOUT, json={"access_token": TOKEN})

    assert response.json() == {"redirect_url": "https://pay.test/checkout/pref-1"}


@pytest.mark.parametrize(
    "payload",
    [{}, {"access_token": None}, {"access_token": 5}, {"access_token": "x" * 201}],
)
async def test_an_invalid_body_is_a_422_validation_error(
    client: AsyncClient, world: World, payload: dict[str, object]
) -> None:
    response = await client.post(CHECKOUT, json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert world.gateway.requests == []


async def test_a_token_of_exactly_200_characters_is_accepted_as_input(client: AsyncClient) -> None:
    response = await client.post(CHECKOUT, json={"access_token": "x" * 200})

    assert response.status_code == 404


async def test_the_token_in_the_query_string_is_not_read(client: AsyncClient) -> None:
    response = await client.post(f"{CHECKOUT}?access_token={TOKEN}")

    assert response.status_code == 422


async def test_the_token_in_a_header_is_not_read(client: AsyncClient) -> None:
    response = await client.post(CHECKOUT, headers={"X-Access-Token": TOKEN})

    assert response.status_code == 422


async def test_the_route_does_not_answer_get(client: AsyncClient) -> None:
    response = await client.get(CHECKOUT, params={"access_token": TOKEN})

    assert response.status_code == 405


async def test_the_response_does_not_echo_the_token(client: AsyncClient) -> None:
    response = await client.post(CHECKOUT, json={"access_token": TOKEN})

    assert TOKEN not in response.text
    assert set(response.json()) == {"redirect_url"}


async def test_the_route_is_public(client: AsyncClient) -> None:
    # No session cookie at all: a guest holds only the order token.
    response = await client.post(CHECKOUT, json={"access_token": TOKEN})

    assert response.status_code == 200


async def test_openapi_declares_the_body_the_statuses_and_no_parameters(
    world: World,
) -> None:
    schema = build(world).openapi()

    operation = schema["paths"][CHECKOUT]["post"]
    assert set(schema["paths"][CHECKOUT]) == {"post"}
    assert "parameters" not in operation
    assert {"200", "404", "409", "422", "503"} <= set(operation["responses"])
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/StartCheckoutRequest"}
    request = schema["components"]["schemas"]["StartCheckoutRequest"]
    assert request["required"] == ["access_token"]
    assert request["properties"]["access_token"]["maxLength"] == 200
    response = schema["components"]["schemas"]["StartCheckoutResponse"]
    assert response["required"] == ["redirect_url"]
    assert operation["tags"] == ["payments"]
