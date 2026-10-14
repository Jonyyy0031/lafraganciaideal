from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.application.queries.stock import (
    GetInventorySettings,
    GetStock,
    ListMovements,
    ListStock,
)
from fragancia_api.modules.inventory.http.router import routers
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import ADMIN_HEADERS, assert_admin_routes_are_protected
from tests.unit.inventory.support import (
    MADE_TO_ORDER,
    STOCKED,
    STOCKED_2,
    UNKNOWN,
    Inventory,
)

BASE = "/api/v1/admin/inventory"
MANAGER = {"Cookie": f"{SESSION_COOKIE}=inventory-manager"}


class InventoryActorResolver:
    """`inventory-manager` holds `inventory:manage`; the shared test admin holds only
    `catalog:manage`."""

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
    services.add(ActorResolver, InventoryActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(RecordMovement, inv.record)
    services.add(SetThreshold, inv.set_threshold)
    services.add(UpdateInventorySettings, inv.update_settings)
    services.add(GetStock, GetStock(inv.catalog, inv.store))
    services.add(ListMovements, ListMovements(inv.catalog, inv.store))
    services.add(ListStock, ListStock(inv.catalog, inv.store))
    services.add(GetInventorySettings, GetInventorySettings(inv.store))
    app = build_app(services, routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def movements_url(presentation_id: UUID) -> str:
    return f"{BASE}/presentations/{presentation_id}/movements"


async def restock(client: AsyncClient, presentation_id: UUID, quantity: int = 10) -> None:
    response = await client.post(
        movements_url(presentation_id),
        json={"kind": "restock", "quantity": quantity, "reason": "Compra"},
        headers=MANAGER,
    )
    assert response.status_code == 201


# --- access ------------------------------------------------------------------------------------

ROUTES = [
    ("POST", f"/presentations/{STOCKED}/movements"),
    ("GET", f"/presentations/{STOCKED}"),
    ("GET", f"/presentations/{STOCKED}/movements"),
    ("PUT", f"/presentations/{STOCKED}/threshold"),
    ("GET", "/stock"),
    ("GET", "/settings"),
    ("PUT", "/settings"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_answers_401_without_a_session(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, f"{BASE}{path}", json={})

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_answers_403_to_an_admin_without_inventory_manage(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, f"{BASE}{path}", json={}, headers=ADMIN_HEADERS)

    assert response.status_code == 403


async def test_an_unknown_session_token_is_401(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/stock", headers={"Cookie": f"{SESSION_COOKIE}=nope"})

    assert response.status_code == 401


# --- POST movements ----------------------------------------------------------------------------


async def test_the_first_restock_answers_201_with_the_id_and_on_hand(
    client: AsyncClient, inv: Inventory
) -> None:
    response = await client.post(
        movements_url(STOCKED),
        json={"kind": "restock", "quantity": 10, "reason": "Compra"},
        headers=MANAGER,
    )

    assert response.status_code == 201
    [movement] = inv.store.movements_log
    assert response.json() == {"id": str(movement.id), "on_hand": 10}
    assert movement.actor_id == "manager-1"


async def test_each_kind_answers_the_resulting_on_hand(client: AsyncClient) -> None:
    await restock(client, STOCKED, 10)

    async def post(kind: str, quantity: int) -> int:
        response = await client.post(
            movements_url(STOCKED),
            json={"kind": kind, "quantity": quantity, "reason": "x"},
            headers=MANAGER,
        )
        assert response.status_code == 201
        return int(response.json()["on_hand"])

    assert await post("loss", 3) == 7
    assert await post("return", 1) == 8
    assert await post("adjustment", -2) == 6
    assert await post("count", 20) == 20


async def test_an_insufficient_loss_is_409_and_changes_nothing(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(client, STOCKED, 10)

    response = await client.post(
        movements_url(STOCKED),
        json={"kind": "loss", "quantity": 50, "reason": "Rotura"},
        headers=MANAGER,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "INVENTORY_STOCK_INSUFFICIENT"
    assert body["details"] == {"on_hand": 10}
    assert inv.store.items[STOCKED].on_hand == 10


async def test_a_made_to_order_presentation_is_422_not_stocked(client: AsyncClient) -> None:
    response = await client.post(
        movements_url(MADE_TO_ORDER),
        json={"kind": "restock", "quantity": 1, "reason": "x"},
        headers=MANAGER,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVENTORY_PRESENTATION_NOT_STOCKED"


async def test_an_unknown_presentation_is_404(client: AsyncClient) -> None:
    response = await client.post(
        movements_url(UNKNOWN),
        json={"kind": "restock", "quantity": 1, "reason": "x"},
        headers=MANAGER,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "INVENTORY_PRESENTATION_NOT_FOUND"


async def test_a_zero_restock_is_422_with_the_quantity_code(client: AsyncClient) -> None:
    response = await client.post(
        movements_url(STOCKED),
        json={"kind": "restock", "quantity": 0, "reason": "x"},
        headers=MANAGER,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVENTORY_QUANTITY_INVALID"


async def test_an_empty_reason_is_422_with_the_reason_code(client: AsyncClient) -> None:
    response = await client.post(
        movements_url(STOCKED),
        json={"kind": "restock", "quantity": 1, "reason": "  "},
        headers=MANAGER,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVENTORY_REASON_INVALID"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "steal", "quantity": 1, "reason": "x"},
        {"kind": "restock", "reason": "x"},
        {"kind": "restock", "quantity": "many", "reason": "x"},
        {"kind": "restock", "quantity": 1},
        {"kind": "restock", "quantity": 1, "reason": "x" * 1001},
        {"kind": "restock", "quantity": 1_000_000_001, "reason": "x"},
    ],
)
async def test_a_malformed_body_is_a_validation_error(
    client: AsyncClient, inv: Inventory, body: dict[str, object]
) -> None:
    response = await client.post(movements_url(STOCKED), json=body, headers=MANAGER)

    assert response.status_code == 422
    assert inv.store.movements_log == []


async def test_a_non_uuid_presentation_id_is_422(client: AsyncClient) -> None:
    response = await client.post(
        f"{BASE}/presentations/not-a-uuid/movements",
        json={"kind": "restock", "quantity": 1, "reason": "x"},
        headers=MANAGER,
    )

    assert response.status_code == 422


# --- GET stock ---------------------------------------------------------------------------------


async def test_get_stock_shows_label_units_and_threshold(client: AsyncClient) -> None:
    await restock(client, STOCKED, 20)
    await client.put(
        f"{BASE}/presentations/{STOCKED}/threshold", json={"threshold": 25}, headers=MANAGER
    )

    response = await client.get(f"{BASE}/presentations/{STOCKED}", headers=MANAGER)

    assert response.status_code == 200
    assert response.json() == {
        "presentation_id": str(STOCKED),
        "perfume_id": str(UUID(int=201)),
        "label": "Versace Eros EDT 50 ml",
        "on_hand": 20,
        "threshold_override": 25,
        "effective_threshold": 25,
        "is_low": True,
    }


async def test_get_stock_of_a_never_counted_presentation_defaults_to_the_global(
    client: AsyncClient,
) -> None:
    response = await client.get(f"{BASE}/presentations/{STOCKED}", headers=MANAGER)

    assert response.status_code == 200
    body = response.json()
    assert (body["on_hand"], body["effective_threshold"], body["is_low"]) == (0, 3, True)
    assert body["threshold_override"] is None


async def test_get_stock_errors(client: AsyncClient) -> None:
    unknown = await client.get(f"{BASE}/presentations/{UNKNOWN}", headers=MANAGER)
    made_to_order = await client.get(f"{BASE}/presentations/{MADE_TO_ORDER}", headers=MANAGER)

    assert (unknown.status_code, unknown.json()["code"]) == (
        404,
        "INVENTORY_PRESENTATION_NOT_FOUND",
    )
    assert (made_to_order.status_code, made_to_order.json()["code"]) == (
        422,
        "INVENTORY_PRESENTATION_NOT_STOCKED",
    )


# --- GET movements -----------------------------------------------------------------------------


async def test_movements_are_listed_with_actor_reason_and_pagination(
    client: AsyncClient, inv: Inventory
) -> None:
    await restock(client, STOCKED, 10)
    inv.clock.current = inv.clock.current.replace(hour=13)
    await client.post(
        movements_url(STOCKED),
        json={"kind": "loss", "quantity": 3, "reason": "Rotura"},
        headers=MANAGER,
    )

    response = await client.get(movements_url(STOCKED), params={"size": 1}, headers=MANAGER)

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["page"], body["size"]) == (2, 1, 1)
    [line] = body["items"]
    assert (line["kind"], line["quantity"], line["delta"], line["on_hand_after"]) == (
        "loss",
        3,
        -3,
        7,
    )
    assert (line["reason"], line["actor_id"]) == ("Rotura", "manager-1")


async def test_movements_of_an_unknown_presentation_is_404(client: AsyncClient) -> None:
    response = await client.get(movements_url(UNKNOWN), headers=MANAGER)

    assert response.status_code == 404
    assert response.json()["code"] == "INVENTORY_PRESENTATION_NOT_FOUND"


@pytest.mark.parametrize(
    "params", [{"page": 0}, {"size": 0}, {"size": 101}, {"page": 100_000_000}, {"page": "x"}]
)
async def test_movements_page_bounds_are_enforced(
    client: AsyncClient, params: dict[str, str | int]
) -> None:
    response = await client.get(movements_url(STOCKED), params=params, headers=MANAGER)

    assert response.status_code == 422


# --- PUT threshold -----------------------------------------------------------------------------


async def test_put_threshold_answers_204_and_null_clears_it(
    client: AsyncClient, inv: Inventory
) -> None:
    url = f"{BASE}/presentations/{STOCKED}/threshold"

    set_ = await client.put(url, json={"threshold": 25}, headers=MANAGER)
    assert (set_.status_code, set_.content) == (204, b"")
    assert (
        inv.store.items[STOCKED].effective_threshold(inv.store.settings.low_stock_threshold) == 25
    )

    cleared = await client.put(url, json={"threshold": None}, headers=MANAGER)
    assert cleared.status_code == 204
    assert inv.store.items[STOCKED].threshold_override is None


async def test_put_threshold_requires_the_field(client: AsyncClient) -> None:
    response = await client.put(
        f"{BASE}/presentations/{STOCKED}/threshold", json={}, headers=MANAGER
    )

    assert response.status_code == 422


async def test_put_threshold_errors(client: AsyncClient) -> None:
    out_of_range = await client.put(
        f"{BASE}/presentations/{STOCKED}/threshold", json={"threshold": 1001}, headers=MANAGER
    )
    unknown = await client.put(
        f"{BASE}/presentations/{UNKNOWN}/threshold", json={"threshold": 5}, headers=MANAGER
    )
    made_to_order = await client.put(
        f"{BASE}/presentations/{MADE_TO_ORDER}/threshold", json={"threshold": 5}, headers=MANAGER
    )

    assert (out_of_range.status_code, out_of_range.json()["code"]) == (
        422,
        "INVENTORY_THRESHOLD_INVALID",
    )
    assert (unknown.status_code, unknown.json()["code"]) == (
        404,
        "INVENTORY_PRESENTATION_NOT_FOUND",
    )
    assert (made_to_order.status_code, made_to_order.json()["code"]) == (
        422,
        "INVENTORY_PRESENTATION_NOT_STOCKED",
    )


# --- GET stock list ----------------------------------------------------------------------------


async def test_stock_list_shows_every_in_stock_presentation_and_excludes_made_to_order(
    client: AsyncClient,
) -> None:
    await restock(client, STOCKED, 10)

    response = await client.get(f"{BASE}/stock", headers=MANAGER)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [(i["label"], i["on_hand"], i["is_low"]) for i in body["items"]] == [
        ("Versace Eros EDT 50 ml", 10, False),
        ("Versace Eros EDT 100 ml", 0, True),
    ]
    assert set(body["items"][0]) == {
        "presentation_id",
        "perfume_id",
        "label",
        "on_hand",
        "effective_threshold",
        "is_low",
    }


async def test_stock_list_low_filter_includes_never_counted(client: AsyncClient) -> None:
    await restock(client, STOCKED, 10)

    response = await client.get(f"{BASE}/stock", params={"low": "true"}, headers=MANAGER)

    assert response.status_code == 200
    assert [i["presentation_id"] for i in response.json()["items"]] == [str(STOCKED_2)]


@pytest.mark.parametrize(
    "params", [{"page": 0}, {"size": 0}, {"size": 101}, {"page": 100_000_000}, {"low": "maybe"}]
)
async def test_stock_list_bounds_are_enforced(
    client: AsyncClient, params: dict[str, str | int]
) -> None:
    response = await client.get(f"{BASE}/stock", params=params, headers=MANAGER)

    assert response.status_code == 422


# --- settings ----------------------------------------------------------------------------------


async def test_settings_default_and_update(client: AsyncClient) -> None:
    default = await client.get(f"{BASE}/settings", headers=MANAGER)
    updated = await client.put(f"{BASE}/settings", json={"low_stock_threshold": 5}, headers=MANAGER)
    after = await client.get(f"{BASE}/settings", headers=MANAGER)

    assert (default.status_code, default.json()) == (200, {"low_stock_threshold": 3})
    assert (updated.status_code, updated.content) == (204, b"")
    assert after.json() == {"low_stock_threshold": 5}


async def test_settings_update_rejects_out_of_range_and_missing(client: AsyncClient) -> None:
    out_of_range = await client.put(
        f"{BASE}/settings", json={"low_stock_threshold": 1001}, headers=MANAGER
    )
    missing = await client.put(f"{BASE}/settings", json={}, headers=MANAGER)

    assert (out_of_range.status_code, out_of_range.json()["code"]) == (
        422,
        "INVENTORY_THRESHOLD_INVALID",
    )
    assert missing.status_code == 422
