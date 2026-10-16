"""HTTP contract of the cart: the cookie, status codes, error shapes, access and OpenAPI."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME
from fragancia_api.modules.orders.http.cart_cookie import CART_COOKIE, CartCookie
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.modules.orders.infrastructure.in_memory import (
    InMemoryCarts,
    InMemoryCatalogOffers,
)
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.support import assert_admin_routes_are_protected
from tests.unit.orders.support import NOW, made_to_order_offer, make_offer, presentation_id
from tests.unit.orders.test_cart_application import SequenceTokens
from tests.unit.orders.test_delivery_http import DeliveryActorResolver

CART = "/api/v1/cart"
MAX_AGE = int(CART_LIFETIME.total_seconds())


def line(n: int) -> str:
    return f"{CART}/lines/{presentation_id(n)}"


class Harness:
    """The cart services over in-memory adapters, with the offers the test sets."""

    def __init__(self, *, secure: bool = False) -> None:
        self.offers = InMemoryCatalogOffers()
        self.carts = InMemoryCarts()
        self.clock = FixedClock()
        self.secure = secure

    def build(self) -> FastAPI:
        tokens = SequenceTokens()
        transactions = InMemoryTransactionRunner()
        services = ServiceRegistry()
        services.add(ActorResolver, DeliveryActorResolver())  # type: ignore[type-abstract]
        services.add(HealthChecks, HealthChecks())
        for command in (SetCartLine, RemoveCartLine, AcknowledgeCartPrices):
            services.add(
                command,
                command(
                    carts=self.carts,
                    tokens=tokens,
                    offers=self.offers,
                    transactions=transactions,
                    clock=self.clock,
                ),
            )
        services.add(
            GetCart,
            GetCart(reader=self.carts, tokens=tokens, offers=self.offers, clock=self.clock),
        )
        services.add(CartCookie, CartCookie(secure=self.secure, max_age_seconds=MAX_AGE))
        app = build_app(services, routers)
        assert_admin_routes_are_protected(app)
        return app


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=harness.build()), base_url="http://t") as c:
        yield c


def cookie_header(response: Response) -> str:
    return response.headers["set-cookie"]


def attributes(header: str) -> dict[str, str]:
    """The `Set-Cookie` attributes, lower-cased; a flag has an empty value."""
    parts = [part.strip() for part in header.split(";")]
    return {key.lower(): value for key, _, value in (part.partition("=") for part in parts[1:])}


# --- the cookie ---------------------------------------------------------------------------------


async def test_an_empty_get_answers_an_empty_cart_and_sets_no_cookie(client: AsyncClient) -> None:
    response = await client.get(CART)

    assert response.status_code == 200
    assert response.json() == {
        "lines": [],
        "subtotal_cents": 0,
        "item_count": 0,
        "has_problems": False,
        "lead_time_max_days": None,
    }
    assert "set-cookie" not in response.headers


async def test_a_get_with_an_unknown_cookie_answers_empty_and_sets_no_cookie(
    client: AsyncClient,
) -> None:
    response = await client.get(CART, headers={"Cookie": f"{CART_COOKIE}=nobody"})

    assert response.status_code == 200
    assert response.json()["lines"] == []
    assert "set-cookie" not in response.headers


async def test_creating_a_cart_sets_the_cookie_with_its_attributes(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})

    response = await client.put(line(1), json={"quantity": 2})

    assert response.status_code == 200
    header = cookie_header(response)
    assert header.startswith(f"{CART_COOKIE}=token-1;")
    found = attributes(header)
    assert found["path"] == "/api/v1"
    assert found["samesite"].lower() == "lax"
    assert found["max-age"] == str(MAX_AGE)
    assert "httponly" in found
    assert "secure" not in found


async def test_the_cookie_is_secure_when_the_cookie_service_says_so() -> None:
    harness = Harness(secure=True)
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})
    async with AsyncClient(
        transport=ASGITransport(app=harness.build()), base_url="http://t"
    ) as client:
        response = await client.put(line(1), json={"quantity": 1})

    assert "secure" in attributes(cookie_header(response))


async def test_a_write_with_the_cookie_refreshes_it(harness: Harness, client: AsyncClient) -> None:
    harness.offers.by_id.update(
        {presentation_id(1): make_offer(1), presentation_id(2): make_offer(2)}
    )
    first = await client.put(line(1), json={"quantity": 1})
    assert first.status_code == 200
    harness.clock.current = NOW + timedelta(days=10)

    second = await client.put(line(2), json={"quantity": 1})

    assert second.status_code == 200
    header = cookie_header(second)
    assert header.startswith(f"{CART_COOKIE}=token-1;")
    assert attributes(header)["max-age"] == str(MAX_AGE)


async def test_removing_and_acknowledging_with_the_cookie_refresh_it(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})
    await client.put(line(1), json={"quantity": 1})

    removed = await client.delete(line(1))
    acknowledged = await client.post(f"{CART}/acknowledge-prices")

    assert cookie_header(removed).startswith(f"{CART_COOKIE}=token-1;")
    assert cookie_header(acknowledged).startswith(f"{CART_COOKIE}=token-1;")


async def test_a_delete_or_acknowledge_without_a_cart_sets_no_cookie(client: AsyncClient) -> None:
    removed = await client.delete(line(1))
    acknowledged = await client.post(f"{CART}/acknowledge-prices")

    assert removed.status_code == acknowledged.status_code == 200
    assert removed.json()["lines"] == acknowledged.json()["lines"] == []
    assert "set-cookie" not in removed.headers
    assert "set-cookie" not in acknowledged.headers


async def test_a_refused_put_sets_no_cookie_and_creates_no_cart(
    harness: Harness, client: AsyncClient
) -> None:
    response = await client.put(line(1), json={"quantity": 1})

    assert response.status_code == 409
    assert "set-cookie" not in response.headers
    assert harness.carts.by_id == {}


# --- add and read back --------------------------------------------------------------------------


async def test_the_cart_survives_requests_through_the_cookie(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1, price_cents=300_000)})

    put = await client.put(line(1), json={"quantity": 2})
    got = await client.get(CART)

    assert put.status_code == got.status_code == 200
    assert got.json() == put.json()
    body = got.json()
    [first] = body["lines"]
    assert first["presentation_id"] == str(presentation_id(1))
    assert (first["status"], first["quantity"]) == ("ok", 2)
    assert (first["unit_price_cents"], first["line_total_cents"]) == (300_000, 600_000)
    assert first["price_changed_from_cents"] is None
    assert (body["subtotal_cents"], body["item_count"], body["has_problems"]) == (600_000, 2, False)


async def test_a_line_exposes_every_field_of_the_view(
    harness: Harness, client: AsyncClient
) -> None:
    offer = made_to_order_offer(1, max_days=7)
    harness.offers.by_id.update({offer.presentation_id: offer})

    body = (await client.put(line(1), json={"quantity": 1})).json()

    assert body["lines"][0] == {
        "presentation_id": str(offer.presentation_id),
        "quantity": 1,
        "status": "ok",
        "label": offer.label,
        "perfume_slug": offer.perfume_slug,
        "card_url": offer.card_url,
        "ml": 100,
        "availability": "made_to_order",
        "lead_time_min_days": 5,
        "lead_time_max_days": 7,
        "available_units": None,
        "unit_price_cents": 250_000,
        "line_total_cents": 250_000,
        "price_changed_from_cents": None,
    }
    assert body["lead_time_max_days"] == 7


# --- errors -------------------------------------------------------------------------------------


async def test_a_quantity_of_eleven_answers_422_with_the_bounds(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1, available_units=50)})

    response = await client.put(line(1), json={"quantity": 11})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ORDERS_CART_QUANTITY_INVALID"
    assert body["details"] == {"min": 1, "max": 10}


async def test_a_quantity_of_zero_answers_422(harness: Harness, client: AsyncClient) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})

    response = await client.put(line(1), json={"quantity": 0})

    assert response.status_code == 422
    assert response.json()["code"] == "ORDERS_CART_QUANTITY_INVALID"


async def test_more_than_the_available_units_answers_409_with_the_available_figure(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1, available_units=3)})

    response = await client.put(line(1), json={"quantity": 4})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "ORDERS_CART_STOCK_UNAVAILABLE"
    assert body["details"] == {"available": 3}


async def test_made_to_order_accepts_ten_regardless_of_stock(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): made_to_order_offer(1)})

    response = await client.put(line(1), json={"quantity": 10})

    assert response.status_code == 200
    assert response.json()["item_count"] == 10


@pytest.mark.parametrize("hidden", [True, False], ids=["hidden", "unknown"])
async def test_a_hidden_or_unknown_presentation_answers_409(
    harness: Harness, client: AsyncClient, hidden: bool
) -> None:
    if hidden:
        harness.offers.by_id.update({presentation_id(1): make_offer(1, is_visible=False)})

    response = await client.put(line(1), json={"quantity": 1})

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_CART_PRESENTATION_UNAVAILABLE"


async def test_a_thirty_first_line_answers_422(harness: Harness, client: AsyncClient) -> None:
    for n in range(1, 32):
        harness.offers.by_id[presentation_id(n)] = make_offer(n)
    for n in range(1, 31):
        assert (await client.put(line(n), json={"quantity": 1})).status_code == 200

    response = await client.put(line(31), json={"quantity": 1})

    assert response.status_code == 422
    assert response.json()["code"] == "ORDERS_CART_TOO_MANY_LINES"


@pytest.mark.parametrize(
    "body", [{}, {"quantity": "two"}, {"quantity": None}, {"quantity": 2.5}], ids=str
)
async def test_a_malformed_body_answers_a_422_validation_error(
    harness: Harness, client: AsyncClient, body: dict[str, object]
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})

    response = await client.put(line(1), json=body)

    assert response.status_code == 422
    assert response.json()["code"] != "ORDERS_CART_QUANTITY_INVALID"


async def test_a_quantity_beyond_the_payload_bound_answers_a_422_validation_error(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})

    response = await client.put(line(1), json={"quantity": 10**12})

    assert response.status_code == 422
    assert harness.carts.by_id == {}


async def test_a_presentation_id_that_is_not_a_uuid_answers_422(client: AsyncClient) -> None:
    response = await client.put(f"{CART}/lines/not-a-uuid", json={"quantity": 1})

    assert response.status_code == 422


# --- price change, remove, acknowledge ----------------------------------------------------------


async def test_a_price_change_shows_and_acknowledging_clears_it(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id[presentation_id(1)] = make_offer(1, price_cents=250_000)
    await client.put(line(1), json={"quantity": 1})
    harness.offers.by_id[presentation_id(1)] = make_offer(1, price_cents=280_000)

    changed = (await client.get(CART)).json()["lines"][0]
    cleared = (await client.post(f"{CART}/acknowledge-prices")).json()["lines"][0]
    after = (await client.get(CART)).json()["lines"][0]

    assert (changed["unit_price_cents"], changed["price_changed_from_cents"]) == (280_000, 250_000)
    assert cleared["price_changed_from_cents"] is None
    assert after["price_changed_from_cents"] is None


async def test_changing_the_quantity_clears_the_price_change(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id[presentation_id(1)] = make_offer(1, price_cents=250_000)
    await client.put(line(1), json={"quantity": 1})
    harness.offers.by_id[presentation_id(1)] = make_offer(1, price_cents=280_000)

    body = (await client.put(line(1), json={"quantity": 2})).json()

    assert body["lines"][0]["price_changed_from_cents"] is None
    assert body["lines"][0]["unit_price_cents"] == 280_000


async def test_a_line_that_sells_out_or_is_hidden_is_flagged_and_left_out_of_the_totals(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update(
        {presentation_id(1): make_offer(1), presentation_id(2): make_offer(2)}
    )
    await client.put(line(1), json={"quantity": 1})
    await client.put(line(2), json={"quantity": 1})
    harness.offers.by_id[presentation_id(1)] = make_offer(1, available_units=0)
    harness.offers.by_id[presentation_id(2)] = make_offer(2, is_visible=False)

    bad = (await client.get(CART)).json()
    harness.offers.by_id[presentation_id(1)] = make_offer(1, available_units=4)
    harness.offers.by_id[presentation_id(2)] = make_offer(2)
    back = (await client.get(CART)).json()

    assert [x["status"] for x in bad["lines"]] == ["sold_out", "unavailable"]
    assert (bad["subtotal_cents"], bad["item_count"], bad["has_problems"]) == (0, 0, True)
    assert [x["status"] for x in back["lines"]] == ["ok", "ok"]
    assert (back["subtotal_cents"], back["has_problems"]) == (500_000, False)


async def test_delete_removes_the_line_and_removing_it_again_is_a_no_op(
    harness: Harness, client: AsyncClient
) -> None:
    harness.offers.by_id.update(
        {presentation_id(1): make_offer(1), presentation_id(2): make_offer(2)}
    )
    await client.put(line(1), json={"quantity": 1})
    await client.put(line(2), json={"quantity": 1})

    first = await client.delete(line(1))
    second = await client.delete(line(1))

    assert first.status_code == second.status_code == 200
    assert [x["presentation_id"] for x in first.json()["lines"]] == [str(presentation_id(2))]
    assert second.json() == first.json()


# --- access -------------------------------------------------------------------------------------


async def test_the_cart_routes_need_no_session(harness: Harness, client: AsyncClient) -> None:
    harness.offers.by_id.update({presentation_id(1): make_offer(1)})

    assert (await client.get(CART)).status_code == 200
    assert (await client.put(line(1), json={"quantity": 1})).status_code == 200
    assert (await client.delete(line(1))).status_code == 200
    assert (await client.post(f"{CART}/acknowledge-prices")).status_code == 200


async def test_an_admin_session_cookie_is_not_read_as_a_cart_token(client: AsyncClient) -> None:
    response = await client.get(CART, headers={"Cookie": "fragancia_session=owner-session"})

    assert response.status_code == 200
    assert response.json()["lines"] == []


# --- OpenAPI ------------------------------------------------------------------------------------


def test_the_openapi_document_declares_the_cart_operations(harness: Harness) -> None:
    document = harness.build().openapi()
    paths = document["paths"]
    by_line = f"{CART}/lines/{{presentation_id}}"

    assert set(paths[CART]) == {"get"}
    assert set(paths[by_line]) == {"put", "delete"}
    assert set(paths[f"{CART}/acknowledge-prices"]) == {"post"}
    assert {"409", "422"} <= set(paths[by_line]["put"]["responses"])
    schemas = document["components"]["schemas"]
    assert {"CartView", "CartLineView", "SetCartLineRequest"} <= set(schemas)
    assert schemas["CartLineView"]["properties"]["status"]["enum"] == [
        "ok",
        "unavailable",
        "sold_out",
        "insufficient_stock",
    ]


def test_the_cart_routes_are_public_not_admin(harness: Harness) -> None:
    paths = harness.build().openapi()["paths"]

    assert all("/admin/" not in path for path in paths if path.startswith(CART))
