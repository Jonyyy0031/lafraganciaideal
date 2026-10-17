"""HTTP contract of `POST /api/v1/checkout` (plan 003 step 5): the 201 shape, the cart cookie
cleared on success and kept on refusal, every 409 and 422 code, and the OpenAPI document."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME
from fragancia_api.modules.orders.http.cart_cookie import CART_COOKIE, CartCookie
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import assert_admin_routes_are_protected
from tests.unit.orders.support import NOW, build_settings, presentation_id
from tests.unit.orders.test_checkout import (
    CART_TOKEN,
    HOME_COST,
    World,
    full_cart,
    in_stock,
)
from tests.unit.orders.test_delivery_http import DeliveryActorResolver

CHECKOUT = "/api/v1/checkout"
COOKIE = {"Cookie": f"{CART_COOKIE}={CART_TOKEN}"}


def body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contact": {"name": "Ana López", "email": "ana@example.com", "phone": "998 123 4567"},
        "delivery_method": "home",
        "address": {
            "street": "Av. Reforma",
            "exterior_number": "100",
            "neighborhood": "Juárez",
            "postal_code": "01000",
            "municipality": "Cuauhtémoc",
            "state": "CMX",
        },
        "expected_total_cents": 618_000,
    }
    payload.update(overrides)
    return payload


class Harness:
    def __init__(self, *, secure: bool = False) -> None:
        self.world = World(build_settings(home_free_from_cents=None))
        self.secure = secure

    def build(self) -> FastAPI:
        services = ServiceRegistry()
        services.add(ActorResolver, DeliveryActorResolver())  # type: ignore[type-abstract]
        services.add(HealthChecks, HealthChecks())
        services.add(PlaceOrder, self.world.place_order())
        services.add(
            CartCookie,
            CartCookie(secure=self.secure, max_age_seconds=int(CART_LIFETIME.total_seconds())),
        )
        app = build_app(services, routers)
        assert_admin_routes_are_protected(app)
        return app


@pytest.fixture
def harness() -> Harness:
    h = Harness()
    full_cart(h.world)
    return h


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=harness.build()), base_url="http://t") as c:
        yield c


def cleared_cart_cookie(header: str) -> bool:
    parts = [part.strip().lower() for part in header.split(";")]
    return (
        parts[0].startswith(f"{CART_COOKIE}=")
        and parts[0] in (f"{CART_COOKIE}=", f'{CART_COOKIE}=""')
        and "max-age=0" in parts
        and "path=/api/v1" in parts
        and "httponly" in parts
        and "samesite=lax" in parts
    )


# --- success ------------------------------------------------------------------------------------


async def test_a_valid_checkout_answers_201_with_the_placed_order(
    client: AsyncClient, harness: Harness
) -> None:
    response = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert response.status_code == 201
    assert response.json() == {
        "number": "LFI-1001",
        "access_token": f"link:{harness.world.orders.orders[0].id}",
        "status": "pending_payment",
        "subtotal_cents": 600_000,
        "delivery_cents": HOME_COST,
        "total_cents": 618_000,
        "payment_due_at": (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "lead_time_max_days": 10,
    }


async def test_success_clears_the_cart_cookie_with_the_same_attributes(
    client: AsyncClient,
) -> None:
    response = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert cleared_cart_cookie(response.headers["set-cookie"]), response.headers["set-cookie"]


async def test_a_secure_cookie_service_clears_the_cookie_as_secure() -> None:
    harness = Harness(secure=True)
    full_cart(harness.world)
    async with AsyncClient(
        transport=ASGITransport(app=harness.build()), base_url="http://t"
    ) as client:
        response = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert "secure" in response.headers["set-cookie"].lower()


async def test_after_a_success_the_same_cookie_gets_the_same_order_back(
    harness: Harness, client: AsyncClient
) -> None:
    first = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    again = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert again.status_code == 201
    assert again.json() == first.json()
    assert cleared_cart_cookie(again.headers["set-cookie"])
    assert len(harness.world.orders.orders) == 1
    assert harness.world.carts.by_id == {}


async def test_the_optional_fields_may_be_left_out_for_pickup(
    harness: Harness, client: AsyncClient
) -> None:
    payload = body(delivery_method="pickup", expected_total_cents=600_000)
    del payload["address"]

    response = await client.post(CHECKOUT, json=payload, headers=COOKIE)

    assert response.status_code == 201
    assert response.json()["delivery_cents"] == 0


# --- 409 ----------------------------------------------------------------------------------------


async def test_a_request_without_the_cookie_answers_409_empty_and_sets_no_cookie(
    client: AsyncClient,
) -> None:
    response = await client.post(CHECKOUT, json=body())

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CHECKOUT_CART_EMPTY"
    assert response.json()["message"] == "Tu carrito está vacío"
    assert "set-cookie" not in response.headers


async def test_an_admin_session_cookie_is_not_read_as_the_cart(client: AsyncClient) -> None:
    response = await client.post(
        CHECKOUT, json=body(), headers={"Cookie": "fragancia_session=owner-session"}
    )

    assert response.json()["code"] == "ORDERS_CHECKOUT_CART_EMPTY"


async def test_a_cart_with_problems_answers_409_listing_the_lines(
    harness: Harness, client: AsyncClient
) -> None:
    harness.world.offers.by_id[presentation_id(1)] = in_stock(1, available_units=0)

    response = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CHECKOUT_CART_HAS_PROBLEMS"
    assert response.json()["details"] == {
        "lines": [{"presentation_id": str(presentation_id(1)), "status": "sold_out"}]
    }


async def test_an_unavailable_delivery_answers_409_with_the_method(client: AsyncClient) -> None:
    payload = body(delivery_method="local", expected_total_cents=609_000)

    response = await client.post(CHECKOUT, json=payload, headers=COOKIE)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE"
    assert response.json()["details"] == {"method": "local"}


async def test_a_wrong_total_answers_409_with_the_real_figures(client: AsyncClient) -> None:
    response = await client.post(CHECKOUT, json=body(expected_total_cents=1), headers=COOKIE)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CHECKOUT_TOTAL_CHANGED"
    assert response.json()["details"] == {
        "subtotal_cents": 600_000,
        "delivery_cents": HOME_COST,
        "total_cents": 618_000,
    }


async def test_a_stock_refusal_answers_409_with_inventorys_lines(
    harness: Harness, client: AsyncClient
) -> None:
    short = [{"presentation_id": str(presentation_id(1)), "requested": 2, "available": 1}]
    harness.world.stocks.short_lines = short

    response = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert response.json()["details"] == {"lines": short}


async def test_a_changed_cart_answers_409(harness: Harness, client: AsyncClient) -> None:
    harness.world.carts.before_lock = lambda carts: carts.by_id.clear()

    response = await client.post(CHECKOUT, json=body(), headers=COOKIE)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CHECKOUT_CART_CHANGED"


@pytest.mark.parametrize(
    "payload",
    [body(expected_total_cents=1), body(delivery_method="local", expected_total_cents=1)],
)
async def test_a_refused_checkout_keeps_the_cookie_and_the_cart(
    harness: Harness, client: AsyncClient, payload: dict[str, Any]
) -> None:
    response = await client.post(CHECKOUT, json=payload, headers=COOKIE)

    assert response.status_code == 409
    assert "set-cookie" not in response.headers
    assert len(harness.world.carts.by_id) == 1
    assert harness.world.orders.orders == []


# --- 422 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        (
            body(contact={"name": "Ana", "email": "ana@example.com", "phone": "123456789"}),
            "contact.phone",
        ),
        (body(contact={"name": "Ana", "email": "nope", "phone": "9981234567"}), "contact.email"),
        (body(address={**body()["address"], "state": "XXX"}), "address.state"),
        (body(address=None), "address"),
        (body(delivery_method="pickup"), "address"),
        (
            body(
                invoice={
                    "rfc": "ABC123",
                    "business_name": "Ana",
                    "tax_regime": "601",
                    "cfdi_use": "G03",
                    "postal_code": "01000",
                }
            ),
            "invoice.rfc",
        ),
        (
            body(
                invoice={
                    "rfc": "XAXX010101000",
                    "business_name": "Ana",
                    "tax_regime": "999",
                    "cfdi_use": "G03",
                    "postal_code": "01000",
                }
            ),
            "invoice.tax_regime",
        ),
        (
            body(
                invoice={
                    "rfc": "XAXX010101000",
                    "business_name": "Ana",
                    "tax_regime": "601",
                    "cfdi_use": "CP01",
                    "postal_code": "01000",
                }
            ),
            "invoice.cfdi_use",
        ),
    ],
)
async def test_invalid_customer_data_answers_422_naming_the_field(
    harness: Harness, client: AsyncClient, payload: dict[str, Any], field: str
) -> None:
    response = await client.post(CHECKOUT, json=payload, headers=COOKIE)

    assert response.status_code == 422
    assert response.json()["code"] == "ORDERS_CUSTOMER_DATA_INVALID"
    assert response.json()["message"] == "Revisa los datos del pedido"
    assert response.json()["details"] == {"field": field}
    assert "set-cookie" not in response.headers
    assert len(harness.world.carts.by_id) == 1


async def test_a_phone_with_the_country_code_is_stored_as_ten_digits(
    harness: Harness, client: AsyncClient
) -> None:
    contact = {"name": "Ana López", "email": "ana@example.com", "phone": "+52 (998) 123-4567"}

    response = await client.post(CHECKOUT, json=body(contact=contact), headers=COOKIE)

    assert response.status_code == 201
    assert harness.world.orders.orders[0].phone.value == "9981234567"


@pytest.mark.parametrize(
    "payload",
    [
        {k: v for k, v in body().items() if k != "contact"},
        {k: v for k, v in body().items() if k != "expected_total_cents"},
        body(delivery_method="courier"),
        body(expected_total_cents="a lot"),
        body(contact={"name": "x" * 501, "email": "a@b.co", "phone": "9981234567"}),
    ],
)
async def test_a_malformed_payload_answers_422_validation_error(
    client: AsyncClient, payload: dict[str, Any]
) -> None:
    response = await client.post(CHECKOUT, json=payload, headers=COOKIE)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "set-cookie" not in response.headers


# --- OpenAPI ------------------------------------------------------------------------------------


def test_the_openapi_document_declares_the_checkout(harness: Harness) -> None:
    document = harness.build().openapi()
    post = document["paths"][CHECKOUT]["post"]

    assert set(document["paths"][CHECKOUT]) == {"post"}
    assert {"201", "409", "422"} <= set(post["responses"])
    assert post["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert post["tags"] == ["orders · checkout"]
    schemas = document["components"]["schemas"]
    assert {
        "CheckoutRequest",
        "CheckoutContact",
        "CheckoutAddress",
        "CheckoutInvoice",
        "PlacedOrder",
    } <= set(schemas)
    assert schemas["CheckoutRequest"]["properties"]["delivery_method"]["enum"] == [
        "home",
        "local",
        "pickup",
    ]
    assert schemas["PlacedOrder"]["properties"]["status"]["const"] == "pending_payment"


def test_the_documented_error_codes_are_all_in_the_route_description(harness: Harness) -> None:
    description = harness.build().openapi()["paths"][CHECKOUT]["post"]["description"]

    for code in (
        "ORDERS_CHECKOUT_CART_EMPTY",
        "ORDERS_CHECKOUT_CART_HAS_PROBLEMS",
        "ORDERS_CHECKOUT_CART_CHANGED",
        "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE",
        "ORDERS_CHECKOUT_TOTAL_CHANGED",
        "ORDERS_CHECKOUT_STOCK_UNAVAILABLE",
        "ORDERS_CUSTOMER_DATA_INVALID",
    ):
        assert code in description
