"""Plan 003, http: the new stock fields, the reservations route and the refused `sale` movement.
There is no route to reserve, commit or release (orders call the facade in process)."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.main.openapi import document
from fragancia_api.modules.inventory.application.commands.stock import RecordMovement
from fragancia_api.modules.inventory.application.queries.stock import (
    GetStock,
    ListMovements,
    ListReservationHolds,
    ListStock,
)
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.modules.inventory.http.router import routers
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.kernel import Ok
from tests.support import ADMIN_HEADERS, assert_admin_routes_are_protected
from tests.unit.inventory.support import MADE_TO_ORDER, STOCKED, STOCKED_2, UNKNOWN, Inventory

BASE = "/api/v1/admin/inventory"
MANAGER = {"Cookie": f"{SESSION_COOKIE}=inventory-manager"}


class _Resolver:
    async def resolve(self, token: str) -> Actor | None:
        if token == "inventory-manager":
            return Actor(
                id="manager-1",
                is_admin=True,
                session_id=UUID(int=2),
                permissions=frozenset({"inventory:manage"}),
            )
        if token == "test-admin-session":
            return Actor(
                id="test-admin",
                is_admin=True,
                session_id=UUID(int=1),
                permissions=frozenset({"catalog:manage"}),
            )
        return None


@pytest.fixture
def inv() -> Inventory:
    return Inventory()


@pytest.fixture
async def client(inv: Inventory) -> AsyncIterator[AsyncClient]:
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(RecordMovement, inv.record)
    services.add(GetStock, GetStock(inv.catalog, inv.store))
    services.add(ListMovements, ListMovements(inv.catalog, inv.store))
    services.add(ListStock, ListStock(inv.catalog, inv.store))
    services.add(ListReservationHolds, ListReservationHolds(inv.catalog, inv.store))
    app = build_app(services, routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def restock(inv: Inventory, presentation_id: UUID, quantity: int) -> None:
    result = await inv.record.execute(
        presentation_id,
        MovementRequest(kind="restock", quantity=quantity, reason="x"),
        "a",
    )
    assert isinstance(result, Ok)


async def reserve(
    inv: Inventory, quantities: dict[UUID, int], reference: str, minutes: int
) -> UUID:
    result = await inv.reserve.execute(
        reference, quantities, inv.clock.now() + timedelta(minutes=minutes)
    )
    assert isinstance(result, Ok)
    return result.value


def holds_url(presentation_id: UUID) -> str:
    return f"{BASE}/presentations/{presentation_id}/reservations"


# --- access ------------------------------------------------------------------------------------


async def test_the_reservations_route_answers_401_without_a_session(client: AsyncClient) -> None:
    response = await client.get(holds_url(STOCKED))

    assert response.status_code == 401


async def test_the_reservations_route_answers_403_without_inventory_manage(
    client: AsyncClient,
) -> None:
    response = await client.get(holds_url(STOCKED), headers=ADMIN_HEADERS)

    assert response.status_code == 403


# --- stock detail and list ---------------------------------------------------------------------


async def test_the_stock_detail_shows_on_hand_reserved_and_available(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(inv, STOCKED, 5)
    await reserve(inv, {STOCKED: 2}, "QA-1", 2)

    response = await client.get(f"{BASE}/presentations/{STOCKED}", headers=MANAGER)

    assert response.status_code == 200
    body = response.json()
    assert (body["on_hand"], body["reserved"], body["available"]) == (5, 2, 3)
    assert body["is_low"] is True  # available 3 <= threshold 3


async def test_the_stock_list_shows_reserved_and_available_and_filters_low_by_available(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(inv, STOCKED, 10)
    await restock(inv, STOCKED_2, 10)
    await reserve(inv, {STOCKED_2: 8}, "QA-1", 2)

    everything = await client.get(f"{BASE}/stock", headers=MANAGER)
    low = await client.get(f"{BASE}/stock", params={"low": "true"}, headers=MANAGER)

    lines = {line["presentation_id"]: line for line in everything.json()["items"]}
    assert (lines[str(STOCKED)]["reserved"], lines[str(STOCKED)]["available"]) == (0, 10)
    assert (lines[str(STOCKED_2)]["reserved"], lines[str(STOCKED_2)]["available"]) == (8, 2)
    assert [line["presentation_id"] for line in low.json()["items"]] == [str(STOCKED_2)]


# --- the reservations route --------------------------------------------------------------------


async def test_the_reservations_route_lists_the_active_holds(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2}, "QA-1", 2)

    response = await client.get(holds_url(STOCKED), headers=MANAGER)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    [hold] = body["items"]
    assert (hold["reservation_id"], hold["reference"], hold["quantity"]) == (
        str(reservation_id),
        "QA-1",
        2,
    )
    assert set(hold) == {"reservation_id", "reference", "quantity", "expires_at", "created_at"}


async def test_the_reservations_route_paginates_soonest_expiry_first(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(inv, STOCKED, 20)
    ids = [await reserve(inv, {STOCKED: 1}, f"QA-{n}", n + 1) for n in range(3)]

    first = await client.get(holds_url(STOCKED), params={"page": 1, "size": 2}, headers=MANAGER)
    second = await client.get(holds_url(STOCKED), params={"page": 2, "size": 2}, headers=MANAGER)

    assert [h["reservation_id"] for h in first.json()["items"]] == [str(i) for i in ids[:2]]
    assert [h["reservation_id"] for h in second.json()["items"]] == [str(ids[2])]
    assert (first.json()["total"], first.json()["page"], first.json()["size"]) == (3, 1, 2)


async def test_the_reservations_route_is_empty_for_a_presentation_without_holds(
    client: AsyncClient,
) -> None:
    response = await client.get(holds_url(STOCKED), headers=MANAGER)

    assert response.status_code == 200
    assert (response.json()["items"], response.json()["total"]) == ([], 0)


async def test_the_reservations_route_of_an_unknown_presentation_is_404(
    client: AsyncClient,
) -> None:
    response = await client.get(holds_url(UNKNOWN), headers=MANAGER)

    assert response.status_code == 404
    assert response.json()["code"] == "INVENTORY_PRESENTATION_NOT_FOUND"


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}, {"page": "x"}])
async def test_the_reservations_route_validates_its_paging(
    client: AsyncClient, params: dict[str, Any]
) -> None:
    response = await client.get(holds_url(STOCKED), params=params, headers=MANAGER)

    assert response.status_code == 422


async def test_the_reservations_route_rejects_a_malformed_presentation_id(
    client: AsyncClient,
) -> None:
    response = await client.get(f"{BASE}/presentations/not-a-uuid/reservations", headers=MANAGER)

    assert response.status_code == 422


# --- the panel cannot record a sale ------------------------------------------------------------


@pytest.mark.parametrize("quantity", [1, 3])
async def test_a_sale_through_the_movements_route_is_422_and_records_nothing(
    client: AsyncClient, inv: Inventory, quantity: int
) -> None:
    await restock(inv, STOCKED, 5)
    movements = len(inv.store.movements_log)

    response = await client.post(
        f"{BASE}/presentations/{STOCKED}/movements",
        json={"kind": "sale", "quantity": quantity, "reason": "Venta"},
        headers=MANAGER,
    )

    assert response.status_code == 422
    assert len(inv.store.movements_log) == movements
    assert inv.store.items[STOCKED].on_hand == 5


async def test_a_sale_movement_shows_in_the_ledger_with_kind_sale(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(inv, STOCKED, 5)
    reservation_id = await reserve(inv, {STOCKED: 2}, "QA-1", 2)
    await inv.commit.execute(reservation_id)

    response = await client.get(f"{BASE}/presentations/{STOCKED}/movements", headers=MANAGER)

    assert response.status_code == 200
    newest = response.json()["items"][0]
    assert (newest["kind"], newest["delta"], newest["reason"], newest["actor_id"]) == (
        "sale",
        -2,
        "Venta: QA-1",
        "system",
    )


async def test_made_to_order_stock_detail_is_still_422(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/presentations/{MADE_TO_ORDER}", headers=MANAGER)

    assert response.status_code == 422


# --- OpenAPI -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schemas() -> dict[str, Any]:
    components: dict[str, Any] = document()["components"]["schemas"]
    return components


def test_openapi_declares_the_reservations_route() -> None:
    operation = document()["paths"][f"{BASE}/presentations/{{presentation_id}}/reservations"]["get"]

    assert operation["operationId"] == "list_stock_reservations"
    assert {"200", "401", "403", "404", "422"} <= operation["responses"].keys()
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReservationHoldPage"
    }


def test_openapi_has_no_route_to_reserve_commit_or_release() -> None:
    paths = [path for path in document()["paths"] if path.startswith(BASE)]

    assert not [p for p in paths if "commit" in p or "release" in p]
    assert not any(
        "post" in operations
        for path, operations in document()["paths"].items()
        if path.startswith(BASE) and path.endswith("/reservations")
    )


def test_openapi_stock_schemas_require_reserved_and_available(schemas: dict[str, Any]) -> None:
    for name in ("StockResponse", "StockLine"):
        assert {"reserved", "available"} <= set(schemas[name]["required"]), name
        assert schemas[name]["properties"]["reserved"]["minimum"] == 0
        assert schemas[name]["properties"]["available"]["minimum"] == 0


def test_openapi_the_ledger_knows_sale_but_the_request_does_not(schemas: dict[str, Any]) -> None:
    assert "sale" in schemas["StockMovement"]["properties"]["kind"]["enum"]
    assert "sale" not in schemas["MovementRequest"]["properties"]["kind"]["enum"]
