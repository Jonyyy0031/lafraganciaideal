"""HTTP contract of the delivery options: access, status codes, error shapes, OpenAPI."""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryDeliverySettings
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import InMemoryTransactionRunner
from tests.support import admin_operations, assert_admin_routes_are_protected

ADMIN = "/api/v1/admin/delivery-options"
PUBLIC = "/api/v1/delivery-options"
OWNER = {"Cookie": f"{SESSION_COOKIE}=owner-session"}
STAFF = {"Cookie": f"{SESSION_COOKIE}=staff-session"}


class DeliveryActorResolver:
    """`owner-session` holds `delivery:manage`; `staff-session` holds what staff holds
    (catalog and inventory), not delivery."""

    async def resolve(self, token: str) -> Actor | None:
        permissions = {
            "owner-session": {"delivery:manage", "catalog:manage", "inventory:manage"},
            "staff-session": {"catalog:manage", "inventory:manage"},
        }.get(token)
        if permissions is None:
            return None
        return Actor(
            id=token, is_admin=True, session_id=UUID(int=3), permissions=frozenset(permissions)
        )


def valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "home": {"enabled": True, "price_cents": 18000, "free_from_cents": 250000},
        "local": {"enabled": True, "price_cents": 9000, "postal_codes": ["77500", "77505"]},
        "pickup": {"enabled": True, "address": "Calle 1 #2", "hours": "Lun-Vie 10-18"},
    }
    body.update(overrides)
    return body


@pytest.fixture
def app() -> FastAPI:
    store = InMemoryDeliverySettings()
    services = ServiceRegistry()
    services.add(ActorResolver, DeliveryActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        UpdateDeliveryOptions,
        UpdateDeliveryOptions(repository=store, transactions=InMemoryTransactionRunner()),
    )
    services.add(GetDeliveryOptions, GetDeliveryOptions(store))
    services.add(GetPublicDeliveryOptions, GetPublicDeliveryOptions(store))
    services.add(QuoteDelivery, QuoteDelivery(store))
    app = build_app(services, routers)
    assert_admin_routes_are_protected(app)
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def configure(client: AsyncClient, body: dict[str, Any] | None = None) -> None:
    response = await client.put(ADMIN, json=body or valid_body(), headers=OWNER)
    assert response.status_code == 204


# --- access ------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_the_admin_routes_answer_401_without_a_session(
    client: AsyncClient, method: str
) -> None:
    response = await client.request(method, ADMIN, json=valid_body() if method == "PUT" else None)

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_the_admin_routes_answer_401_for_an_unknown_session(
    client: AsyncClient, method: str
) -> None:
    headers = {"Cookie": f"{SESSION_COOKIE}=nobody"}

    response = await client.request(
        method, ADMIN, json=valid_body() if method == "PUT" else None, headers=headers
    )

    assert response.status_code == 401


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_the_admin_routes_answer_403_without_the_delivery_permission(
    client: AsyncClient, method: str
) -> None:
    response = await client.request(
        method, ADMIN, json=valid_body() if method == "PUT" else None, headers=STAFF
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_a_forbidden_put_does_not_change_the_configuration(client: AsyncClient) -> None:
    await client.put(ADMIN, json=valid_body(), headers=STAFF)

    public = await client.get(PUBLIC)

    assert public.json() == {"home": None, "local": None, "pickup": None}


async def test_the_public_routes_need_no_session(client: AsyncClient) -> None:
    assert (await client.get(PUBLIC)).status_code == 200
    assert (await client.get(f"{PUBLIC}/quote", params={"subtotal_cents": 1})).status_code == 200


def test_the_admin_delivery_routes_exist_under_the_admin_prefix(app: FastAPI) -> None:
    operations = admin_operations(app)

    assert ("GET", ADMIN) in operations
    assert ("PUT", ADMIN) in operations


# --- admin GET / PUT ---------------------------------------------------------------------------


async def test_the_owner_reads_the_default_configuration(client: AsyncClient) -> None:
    response = await client.get(ADMIN, headers=OWNER)

    assert response.status_code == 200
    assert response.json() == {
        "home": {"enabled": False, "price_cents": 0, "free_from_cents": None},
        "local": {"enabled": False, "price_cents": 0, "postal_codes": []},
        "pickup": {"enabled": False, "address": "", "hours": ""},
    }


async def test_the_owner_saves_with_204_and_reads_back_the_same_values(
    client: AsyncClient,
) -> None:
    response = await client.put(ADMIN, json=valid_body(), headers=OWNER)

    assert response.status_code == 204
    assert response.content == b""
    assert (await client.get(ADMIN, headers=OWNER)).json() == valid_body()


async def test_put_replaces_the_whole_configuration(client: AsyncClient) -> None:
    await configure(client)
    off = valid_body(
        home={"enabled": False, "price_cents": 500, "free_from_cents": None},
        local={"enabled": False, "price_cents": 0, "postal_codes": []},
        pickup={"enabled": False, "address": "", "hours": ""},
    )

    await configure(client, off)

    assert (await client.get(ADMIN, headers=OWNER)).json() == off


# --- validation (422) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            valid_body(local={"enabled": True, "price_cents": 9000, "postal_codes": []}),
            "local.postal_codes",
        ),
        (
            valid_body(pickup={"enabled": True, "address": "", "hours": "Lun-Vie"}),
            "pickup.address",
        ),
        (
            valid_body(pickup={"enabled": True, "address": "Calle", "hours": "  "}),
            "pickup.hours",
        ),
        (
            valid_body(home={"enabled": True, "price_cents": -1, "free_from_cents": None}),
            "home.price_cents",
        ),
        (
            valid_body(home={"enabled": True, "price_cents": 1_000_001, "free_from_cents": None}),
            "home.price_cents",
        ),
        (
            valid_body(local={"enabled": True, "price_cents": -5, "postal_codes": ["77500"]}),
            "local.price_cents",
        ),
        (
            valid_body(local={"enabled": True, "price_cents": 9000, "postal_codes": ["7750"]}),
            "local.postal_codes",
        ),
        (
            valid_body(home={"enabled": True, "price_cents": 1, "free_from_cents": 0}),
            "home.free_from_cents",
        ),
    ],
    ids=[
        "local-on-without-codes",
        "pickup-on-empty-address",
        "pickup-on-blank-hours",
        "negative-home-price",
        "home-price-over-the-bound",
        "negative-local-price",
        "four-digit-postal-code",
        "free-from-zero",
    ],
)
async def test_a_broken_rule_answers_422_naming_the_field(
    client: AsyncClient, body: dict[str, Any], field: str
) -> None:
    response = await client.put(ADMIN, json=body, headers=OWNER)

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert payload["details"]["field"] == field


async def test_an_invalid_postal_code_in_the_put_is_named_by_index_and_value(
    client: AsyncClient,
) -> None:
    body = valid_body(
        local={"enabled": True, "price_cents": 9000, "postal_codes": ["77500", "7750", "bad"]}
    )

    response = await client.put(ADMIN, json=body, headers=OWNER)

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert payload["details"] == {"field": "local.postal_codes", "index": 1, "value": "7750"}


async def test_the_reported_postal_code_value_is_truncated_to_twenty_characters(
    client: AsyncClient,
) -> None:
    body = valid_body(local={"enabled": True, "price_cents": 9000, "postal_codes": ["z" * 30]})

    response = await client.put(ADMIN, json=body, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["details"] == {
        "field": "local.postal_codes",
        "index": 0,
        "value": "z" * 20,
    }


async def test_a_rejected_put_keeps_the_previous_configuration(client: AsyncClient) -> None:
    await configure(client)
    broken = valid_body(local={"enabled": True, "price_cents": 9000, "postal_codes": []})

    await client.put(ADMIN, json=broken, headers=OWNER)

    assert (await client.get(ADMIN, headers=OWNER)).json() == valid_body()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"home": valid_body()["home"]},
        valid_body(home={"enabled": "maybe", "price_cents": 1, "free_from_cents": None}),
        valid_body(home={"enabled": True, "price_cents": "cheap", "free_from_cents": None}),
        valid_body(local={"enabled": True, "price_cents": 1, "postal_codes": "77500"}),
        valid_body(pickup={"enabled": True, "address": 5, "hours": "x"}),
    ],
    ids=[
        "empty",
        "missing-methods",
        "enabled-not-a-bool",
        "price-not-a-number",
        "codes-not-a-list",
        "address-not-a-string",
    ],
)
async def test_a_malformed_body_answers_the_generic_validation_error(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.put(ADMIN, json=body, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "body",
    [
        valid_body(
            local={
                "enabled": True,
                "price_cents": 1,
                "postal_codes": [f"{n:05d}" for n in range(1001)],
            }
        ),
        valid_body(pickup={"enabled": True, "address": "a" * 1001, "hours": "x"}),
        valid_body(pickup={"enabled": True, "address": "x", "hours": "h" * 1001}),
        valid_body(home={"enabled": True, "price_cents": 1_000_000_001, "free_from_cents": None}),
    ],
    ids=["over-1000-codes", "address-over-1000", "hours-over-1000", "price-over-the-payload-bound"],
)
async def test_the_payload_bounds_answer_the_generic_validation_error(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.put(ADMIN, json=body, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --- public GET --------------------------------------------------------------------------------


async def test_the_public_list_is_empty_by_default(client: AsyncClient) -> None:
    response = await client.get(PUBLIC)

    assert response.status_code == 200
    assert response.json() == {"home": None, "local": None, "pickup": None}


async def test_the_public_list_shows_the_three_enabled_methods(client: AsyncClient) -> None:
    await configure(client)

    response = await client.get(PUBLIC)

    assert response.json() == {
        "home": {"price_cents": 18000, "free_from_cents": 250000},
        "local": {"price_cents": 9000, "postal_codes": ["77500", "77505"]},
        "pickup": {"address": "Calle 1 #2", "hours": "Lun-Vie 10-18"},
    }


async def test_switching_home_off_removes_it_from_the_public_list_and_keeps_its_price(
    client: AsyncClient,
) -> None:
    await configure(client)
    off = valid_body(home={"enabled": False, "price_cents": 18000, "free_from_cents": 250000})

    await configure(client, off)

    assert (await client.get(PUBLIC)).json()["home"] is None
    assert (await client.get(ADMIN, headers=OWNER)).json()["home"] == {
        "enabled": False,
        "price_cents": 18000,
        "free_from_cents": 250000,
    }


async def test_the_public_list_does_not_leak_the_enabled_flag_or_a_disabled_method(
    client: AsyncClient,
) -> None:
    await configure(client, valid_body(pickup={"enabled": False, "address": "x", "hours": "y"}))

    payload = (await client.get(PUBLIC)).json()

    assert payload["pickup"] is None
    assert "enabled" not in payload["home"]


# --- public quote ------------------------------------------------------------------------------


def quote_params(postal_code: str | None, subtotal_cents: int | str) -> dict[str, Any]:
    params: dict[str, Any] = {"subtotal_cents": subtotal_cents}
    if postal_code is not None:
        params["postal_code"] = postal_code
    return params


async def test_the_quote_for_the_default_configuration_is_empty(client: AsyncClient) -> None:
    response = await client.get(f"{PUBLIC}/quote", params=quote_params("77500", 100000))

    assert response.status_code == 200
    assert response.json() == {"options": []}


async def test_the_quote_lists_the_methods_that_apply_in_order(client: AsyncClient) -> None:
    await configure(client)

    response = await client.get(f"{PUBLIC}/quote", params=quote_params("77500", 100000))

    assert response.json() == {
        "options": [
            {"method": "home", "cost_cents": 18000},
            {"method": "local", "cost_cents": 9000},
            {"method": "pickup", "cost_cents": 0},
        ]
    }


async def test_the_quote_makes_home_free_at_the_free_from_subtotal(client: AsyncClient) -> None:
    await configure(client)

    response = await client.get(f"{PUBLIC}/quote", params=quote_params("77500", 250000))

    assert response.json()["options"][0] == {"method": "home", "cost_cents": 0}


async def test_the_quote_omits_local_for_a_postal_code_off_the_list(client: AsyncClient) -> None:
    await configure(client)

    response = await client.get(f"{PUBLIC}/quote", params=quote_params("01000", 100000))

    assert [line["method"] for line in response.json()["options"]] == ["home", "pickup"]


async def test_the_quote_without_a_postal_code_lists_home_and_pickup(client: AsyncClient) -> None:
    await configure(client)

    response = await client.get(f"{PUBLIC}/quote", params=quote_params(None, 100000))

    assert [line["method"] for line in response.json()["options"]] == ["home", "pickup"]


async def test_the_quote_drops_home_once_it_is_switched_off(client: AsyncClient) -> None:
    await configure(client)
    await configure(
        client,
        valid_body(home={"enabled": False, "price_cents": 18000, "free_from_cents": 250000}),
    )

    response = await client.get(f"{PUBLIC}/quote", params=quote_params("77500", 100000))

    assert [line["method"] for line in response.json()["options"]] == ["local", "pickup"]


async def test_a_malformed_postal_code_answers_422_postal_code_invalid(
    client: AsyncClient,
) -> None:
    response = await client.get(f"{PUBLIC}/quote", params=quote_params("abc", 100000))

    assert response.status_code == 422
    assert response.json()["code"] == "ORDERS_POSTAL_CODE_INVALID"


async def test_an_empty_postal_code_is_malformed_not_absent(client: AsyncClient) -> None:
    response = await client.get(f"{PUBLIC}/quote", params=quote_params("", 100000))

    assert response.status_code == 422
    assert response.json()["code"] == "ORDERS_POSTAL_CODE_INVALID"


@pytest.mark.parametrize("subtotal", [-1, 100_000_001])
async def test_a_subtotal_out_of_range_answers_422_naming_the_field(
    client: AsyncClient, subtotal: int
) -> None:
    response = await client.get(f"{PUBLIC}/quote", params=quote_params("77500", subtotal))

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert payload["details"] == {"field": "subtotal_cents", "min": 0, "max": 100_000_000}


async def test_a_missing_subtotal_answers_the_generic_validation_error(
    client: AsyncClient,
) -> None:
    response = await client.get(f"{PUBLIC}/quote", params={"postal_code": "77500"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_a_non_numeric_subtotal_answers_the_generic_validation_error(
    client: AsyncClient,
) -> None:
    response = await client.get(f"{PUBLIC}/quote", params=quote_params("77500", "lots"))

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --- OpenAPI -----------------------------------------------------------------------------------


def test_the_openapi_document_declares_the_delivery_operations(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    assert set(paths[ADMIN]) == {"get", "put"}
    assert "204" in paths[ADMIN]["put"]["responses"]
    assert "422" in paths[ADMIN]["put"]["responses"]
    assert set(paths[PUBLIC]) == {"get"}
    assert set(paths[f"{PUBLIC}/quote"]) == {"get"}
    assert "security" not in paths[PUBLIC]["get"]
    assert "security" not in paths[f"{PUBLIC}/quote"]["get"]
    quote_parameters = {p["name"]: p for p in paths[f"{PUBLIC}/quote"]["get"]["parameters"]}
    assert quote_parameters["subtotal_cents"]["required"] is True
    assert quote_parameters["postal_code"]["required"] is False
