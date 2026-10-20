"""Plan 005 steps 4 and 5, http: the three public customer routes. No session needed, no route
carrying the token in its path (decision 42), the exact key set of the view (no internal field),
wrong email and unknown number answering byte-identical bodies, every status and error code, the
client IP passed to the throttle, validation, and the OpenAPI document."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.commands.customer_order import (
    CancelOrderByCustomer,
    LookUpOrder,
)
from fragancia_api.modules.orders.application.commands.lifecycle import (
    CancelUnpaidOrder,
    MarkOrderPaid,
)
from fragancia_api.modules.orders.application.queries.customer_order import GetCustomerOrder
from fragancia_api.modules.orders.domain.order import (
    LOOKUP_IP_MAX,
    LOOKUP_NUMBER_MAX,
    OrderStatus,
)
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import assert_admin_routes_are_protected
from tests.unit.orders.support import NOW
from tests.unit.orders.test_customer_order import (
    ADDRESS_KEYS,
    FORBIDDEN,
    LINE_KEYS,
    LINKS,
    STEP_KEYS,
    VIEW_KEYS,
    Shop,
    token_of,
)
from tests.unit.orders.test_order_lifecycle_http import OrdersActorResolver

BASE = "/api/v1/orders"
LOOKUP = f"{BASE}/lookup"
VIEW = f"{BASE}/view"
CANCEL = f"{BASE}/cancel"
S = OrderStatus


def access(token: str) -> dict[str, str]:
    return {"access_token": token}


def lookup_body(number: str = "LFI-1001", email: str = "ana@example.com") -> dict[str, Any]:
    return {"number": number, "email": email}


class Harness:
    def __init__(self) -> None:
        self.shop = Shop()

    def build(self) -> FastAPI:
        shop = self.shop
        services = ServiceRegistry()
        services.add(ActorResolver, OrdersActorResolver())  # type: ignore[type-abstract]
        services.add(HealthChecks, HealthChecks())
        services.add(GetCustomerOrder, shop.view())
        services.add(LookUpOrder, shop.lookup())
        services.add(CancelOrderByCustomer, shop.cancel())
        app = build_app(services, routers)
        assert_admin_routes_are_protected(app)
        return app


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def app(harness: Harness) -> FastAPI:
    return harness.build()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


# --- POST /view ------------------------------------------------------------------------------


async def test_the_view_is_public_and_answers_200_for_a_known_token(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(VIEW, json=access(token_of()))

    assert response.status_code == 200
    body = response.json()
    assert (body["number"], body["status"], body["can_cancel"]) == (
        "LFI-1001",
        "pending_payment",
        True,
    )
    assert [s["status"] for s in body["timeline"]] == ["pending_payment"]
    assert body["timeline"][0]["at"].startswith("2026-10-02T12:00:00")


async def test_the_view_has_exactly_the_customer_keys_and_nothing_internal(
    client: AsyncClient, harness: Harness
) -> None:
    shop = harness.shop
    await shop.store()
    await shop.world.wire(MarkOrderPaid).execute(1001, "Transferencia directa", "owner-1")
    await shop.world.orders.add_note(1001, "owner-1", "nota interna", NOW)

    body = (await client.post(VIEW, json=access(token_of()))).json()

    assert set(body) == VIEW_KEYS
    assert set(body["lines"][0]) == LINE_KEYS
    assert set(body["address"]) == ADDRESS_KEYS
    assert all(set(step) == STEP_KEYS for step in body["timeline"])
    assert FORBIDDEN.isdisjoint(body)


async def test_the_view_body_never_contains_an_internal_value(
    client: AsyncClient, harness: Harness
) -> None:
    shop = harness.shop
    await shop.store()
    await shop.world.orders.add_note(1001, "owner-1", "nota interna secreta", NOW)
    await shop.world.wire(CancelUnpaidOrder).execute(1001, "motivo privado", "owner-1")
    stored = shop.world.current()
    assert stored.access_token_digest is not None
    assert stored.cart_token_digest is not None

    text = (await client.post(VIEW, json=access(token_of()))).text

    for secret in (
        "nota interna secreta",
        "motivo privado",
        "owner-1",
        str(stored.reservation_id),
        stored.access_token_digest,
        stored.cart_token_digest,
        "ana@example.com",
    ):
        assert secret not in text, secret
    assert '"cancelled_by":"shop"' in text.replace(" ", "")


async def test_a_wrong_token_answers_404_order_not_found(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(VIEW, json=access("wrong"))

    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_ORDER_NOT_FOUND"


async def test_the_stored_digest_does_not_open_the_order(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(VIEW, json=access(LINKS.digest(token_of())))

    assert response.status_code == 404


async def test_a_token_over_200_characters_is_a_validation_error(client: AsyncClient) -> None:
    response = await client.post(VIEW, json=access("t" * 201))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --- POST /lookup and the token-free paths ---------------------------------------------------


def test_no_orders_route_carries_a_token_in_its_path(app: FastAPI) -> None:
    """Decision 42: the token is in the body, so no log of the path ever records it."""
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith(f"{BASE}/") and path != BASE:
            continue
        assert "token" not in path.lower(), path
        for operation in operations.values():
            names = [p["name"] for p in operation.get("parameters", []) if p["in"] == "path"]
            assert not any("token" in name for name in names), (path, names)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", f"{BASE}/tok-1"),
        ("post", f"{BASE}/tok-1/cancel"),
        ("get", f"{BASE}/{'t' * 201}"),
    ],
)
async def test_the_old_token_in_path_routes_are_gone(
    client: AsyncClient, harness: Harness, method: str, path: str
) -> None:
    await harness.shop.store()

    response = await client.request(method, path)

    assert response.status_code in (404, 405)
    assert harness.shop.world.current().status is S.PENDING_PAYMENT


@pytest.mark.parametrize("path", [VIEW, CANCEL])
async def test_the_token_routes_only_accept_post(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 405


@pytest.mark.parametrize("path", [VIEW, CANCEL])
@pytest.mark.parametrize("payload", [{}, {"access_token": 5}, {"access_token": None}])
async def test_the_token_routes_validate_their_body(
    client: AsyncClient, path: str, payload: dict[str, Any]
) -> None:
    response = await client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_the_view_shows_the_name_and_phone_of_the_customer(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    body = (await client.post(VIEW, json=access(token_of()))).json()

    assert (body["customer_name"], body["customer_phone"]) == ("Ana López", "9981234567")


async def test_lookup_answers_the_token_that_opens_the_same_order(
    client: AsyncClient, harness: Harness
) -> None:
    order = await harness.shop.store()

    response = await client.post(LOOKUP, json=lookup_body())

    assert response.status_code == 200
    assert response.json() == {"access_token": LINKS.token(order.id)}


async def test_lookup_is_not_captured_by_the_token_route(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(LOOKUP, json=lookup_body("LFI-9999"))

    # The lookup's own error, not the token route's ORDERS_ORDER_NOT_FOUND or a 405.
    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_LOOKUP_NOT_FOUND"


async def test_lookup_validates_its_own_body_not_a_token_path(client: AsyncClient) -> None:
    response = await client.post(LOOKUP, json={"number": "LFI-1001"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_get_on_lookup_is_405_now_that_no_route_has_a_token_path(
    client: AsyncClient,
) -> None:
    response = await client.get(LOOKUP)

    assert response.status_code == 405


async def test_lookup_accepts_the_number_without_prefix_and_the_email_in_any_case(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(LOOKUP, json=lookup_body("1001", "  ANA@Example.COM "))

    assert response.status_code == 200


async def test_a_wrong_email_and_an_unknown_number_answer_byte_identical_responses(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    wrong_email = await client.post(LOOKUP, json=lookup_body("LFI-1001", "otro@example.com"))
    unknown = await client.post(LOOKUP, json=lookup_body("LFI-9999", "ana@example.com"))

    assert wrong_email.status_code == unknown.status_code == 404
    assert wrong_email.content == unknown.content
    assert wrong_email.headers["content-type"] == unknown.headers["content-type"]
    assert wrong_email.headers["content-length"] == unknown.headers["content-length"]
    assert wrong_email.json() == {
        "code": "ORDERS_LOOKUP_NOT_FOUND",
        "message": "No encontramos un pedido con ese número y correo",
    }


@pytest.mark.parametrize(
    ("number", "email"), [("abc", "ana@example.com"), ("LFI-1001", "not-an-email")]
)
async def test_unparsable_input_answers_the_same_bytes_as_a_wrong_email(
    client: AsyncClient, harness: Harness, number: str, email: str
) -> None:
    await harness.shop.store()
    reference = await client.post(LOOKUP, json=lookup_body("LFI-1001", "otro@example.com"))

    response = await client.post(LOOKUP, json=lookup_body(number, email))

    assert response.status_code == 404
    assert response.content == reference.content


async def test_the_sixth_attempt_for_one_number_answers_429_with_its_code(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()
    for _ in range(LOOKUP_NUMBER_MAX):
        assert (
            await client.post(LOOKUP, json=lookup_body(email="x@example.com"))
        ).status_code == 404

    response = await client.post(LOOKUP, json=lookup_body())

    assert response.status_code == 429
    assert response.json() == {
        "code": "ORDERS_LOOKUP_RATE_LIMITED",
        "message": "Demasiados intentos; espera unos minutos",
    }


async def test_the_ip_limit_answers_429_on_the_twenty_first_attempt(client: AsyncClient) -> None:
    for i in range(LOOKUP_IP_MAX):
        response = await client.post(LOOKUP, json=lookup_body(f"LFI-{3000 + i}"))
        assert response.status_code == 404

    response = await client.post(LOOKUP, json=lookup_body("LFI-4000"))

    assert response.status_code == 429


async def test_the_throttle_counts_the_client_ip_of_the_request(
    client: AsyncClient, harness: Harness
) -> None:
    await client.post(LOOKUP, json=lookup_body())

    assert set(harness.shop.throttle.counters) == {"lookup-ip:127.0.0.1", "lookup-number:1001"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"number": "LFI-1001"},
        {"email": "ana@example.com"},
        {"number": "x" * 101, "email": "ana@example.com"},
        {"number": "LFI-1001", "email": "a" * 301},
        {"number": 1001, "email": "ana@example.com"},
    ],
)
async def test_lookup_validates_its_body(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post(LOOKUP, json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_validation_error_does_not_count_an_attempt(
    client: AsyncClient, harness: Harness
) -> None:
    await client.post(LOOKUP, json={"number": "LFI-1001"})

    assert harness.shop.throttle.counters == {}


# --- POST /cancel ---------------------------------------------------------------------------


async def test_cancel_answers_204_with_no_body_and_cancels_the_order(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(CANCEL, json=access(token_of()))

    assert response.status_code == 204
    assert response.content == b""
    assert harness.shop.world.current().status is S.CANCELLED
    view = (await client.post(VIEW, json=access(token_of()))).json()
    assert (view["status"], view["cancelled_by"], view["can_cancel"]) == (
        "cancelled",
        "customer",
        False,
    )


async def test_cancel_with_an_unknown_token_answers_404(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()

    response = await client.post(CANCEL, json=access("wrong"))

    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_ORDER_NOT_FOUND"
    assert harness.shop.world.current().status is S.PENDING_PAYMENT


async def test_cancel_of_a_paid_order_answers_409_transition_invalid(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store(S.PAID)

    response = await client.post(CANCEL, json=access(token_of()))

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_TRANSITION_INVALID"
    assert harness.shop.world.current().status is S.PAID


async def test_cancel_twice_answers_409_the_second_time(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store()
    await client.post(CANCEL, json=access(token_of()))

    response = await client.post(CANCEL, json=access(token_of()))

    assert response.status_code == 409


async def test_a_cancel_of_a_lookup_token_works_end_to_end(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.shop.store(due=timedelta(minutes=30))
    token = (await client.post(LOOKUP, json=lookup_body())).json()["access_token"]
    # The fake links digest `link:<id>` the same way the stored digest was made from `tok-1`;
    # re-point the order at the token the lookup answered, as the real HMAC links do.
    harness.shop.world.current().access_token_digest = LINKS.digest(token)

    assert (await client.post(VIEW, json=access(token))).status_code == 200
    assert (await client.post(CANCEL, json=access(token))).status_code == 204


# --- OpenAPI -----------------------------------------------------------------------------------


def test_the_three_customer_operations_are_in_the_openapi(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    assert "post" in paths[LOOKUP]
    assert "post" in paths[VIEW]
    assert "post" in paths[CANCEL]


def test_the_openapi_declares_every_status_of_the_customer_routes(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    lookup = paths[LOOKUP]["post"]["responses"]
    view = paths[VIEW]["post"]["responses"]
    cancel = paths[CANCEL]["post"]["responses"]
    assert {"200", "404", "422", "429"} <= lookup.keys()
    assert {"200", "404", "422"} <= view.keys()
    assert {"204", "404", "409", "422"} <= cancel.keys()


def test_the_customer_routes_are_public_and_tagged(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    for path, method in [
        (LOOKUP, "post"),
        (VIEW, "post"),
        (CANCEL, "post"),
    ]:
        assert paths[path][method]["tags"] == ["orders · customer"], path
        assert "security" not in paths[path][method], path


def test_the_openapi_view_schema_has_exactly_the_customer_keys(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert set(schemas["CustomerOrderView"]["properties"]) == VIEW_KEYS
    assert set(schemas["CustomerOrderLine"]["properties"]) == LINE_KEYS
    assert set(schemas["CustomerOrderStep"]["properties"]) == STEP_KEYS
    assert set(schemas["OrderLookupRequest"]["properties"]) == {"number", "email"}
    assert set(schemas["OrderAccessRequest"]["properties"]) == {"access_token"}
    assert set(schemas["OrderLookupResponse"]["properties"]) == {"access_token"}
    assert schemas["CustomerOrderView"]["properties"]["cancelled_by"]["anyOf"][0]["enum"] == [
        "customer",
        "system",
        "shop",
    ]
