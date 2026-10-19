"""Plan 004 step 6, http: the admin order routes. Access per route (anonymous 401, no orders
permission 403, staff 403 on the finance routes), order number parsing, status codes, error codes
and shapes, the actor taken from the session, and the OpenAPI document."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.commands.lifecycle import (
    AddOrderNote,
    AdvanceOrder,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderPaid,
    MarkOrderRefunded,
)
from fragancia_api.modules.orders.application.queries.orders import GetOrder, ListOrders
from fragancia_api.modules.orders.contracts import (
    AdminOrderDetail,
    AdminOrderEvent,
    AdminOrderNote,
    AdminOrderPage,
    AdminOrderSummary,
    OrderStatusName,
)
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod
from fragancia_api.modules.orders.domain.order import OrderStatus
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import admin_operations, assert_admin_routes_are_protected
from tests.unit.orders.support import NOW
from tests.unit.orders.test_order_lifecycle import World, refusal

BASE = "/api/v1/admin/orders"
OWNER = {"Cookie": f"{SESSION_COOKIE}=owner-session"}
STAFF = {"Cookie": f"{SESSION_COOKIE}=staff-session"}
CATALOG_ONLY = {"Cookie": f"{SESSION_COOKIE}=catalog-session"}
FINANCE_ONLY = {"Cookie": f"{SESSION_COOKIE}=finance-session"}
S = OrderStatus


class OrdersActorResolver:
    """The owner holds both order permissions, staff only `orders:manage`, `catalog-session`
    neither, `finance-session` only `orders:finance`."""

    async def resolve(self, token: str) -> Actor | None:
        permissions = {
            "owner-session": {"orders:manage", "orders:finance", "catalog:manage"},
            "staff-session": {"orders:manage", "catalog:manage", "inventory:manage"},
            "catalog-session": {"catalog:manage"},
            "finance-session": {"orders:finance"},
        }.get(token)
        if permissions is None:
            return None
        return Actor(
            id=token, is_admin=True, session_id=UUID(int=3), permissions=frozenset(permissions)
        )


class FakeQueries:
    """The `OrderQueries` port over one canned detail; records what the routes passed."""

    def __init__(self) -> None:
        self.page_calls: list[tuple[str | None, str | None, int, int]] = []
        self.detail_calls: list[int] = []
        self.known = {1001}

    async def page(
        self, status: OrderStatusName | None, q: str | None, page: int, size: int
    ) -> AdminOrderPage:
        self.page_calls.append((status, q, page, size))
        summary = AdminOrderSummary(
            number="LFI-1001",
            created_at=NOW,
            customer_name="Ana López",
            customer_email="ana@example.com",
            status="pending_payment",
            delivery_method="home",
            total_cents=268_000,
            refund_status=None,
        )
        return AdminOrderPage(items=[summary], total=1, page=page, size=size)

    async def detail(self, number: int) -> AdminOrderDetail | None:
        self.detail_calls.append(number)
        if number not in self.known:
            return None
        return AdminOrderDetail(
            number=f"LFI-{number}",
            status="paid",
            created_at=NOW,
            updated_at=NOW,
            payment_due_at=NOW,
            customer_name="Ana López",
            customer_email="ana@example.com",
            customer_phone="+529981234567",
            delivery_method="pickup",
            delivery_cents=0,
            address=None,
            pickup_address="Calle 1",
            pickup_hours="10-18",
            invoice=None,
            lines=[],
            subtotal_cents=0,
            total_cents=0,
            lead_time_max_days=None,
            cancel_reason=None,
            refund_status=None,
            shipping_carrier=None,
            tracking_number=None,
            reservation_id=None,
            history=[
                AdminOrderEvent(
                    at=NOW,
                    from_status="pending_payment",
                    to_status="paid",
                    actor="owner-session",
                    note="Transferencia",
                    carrier=None,
                    tracking_number=None,
                )
            ],
            notes=[AdminOrderNote(at=NOW, actor="staff-session", text="Hola")],
        )


class Harness:
    def __init__(self) -> None:
        self.world = World()
        self.world.clock.current = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
        self.queries = FakeQueries()

    def build(self) -> FastAPI:
        services = ServiceRegistry()
        services.add(ActorResolver, OrdersActorResolver())  # type: ignore[type-abstract]
        services.add(HealthChecks, HealthChecks())
        for command in (
            MarkOrderPaid,
            AdvanceOrder,
            CancelUnpaidOrder,
            CancelPaidOrder,
            MarkOrderRefunded,
        ):
            services.add(command, self.world.wire(command))
        services.add(AddOrderNote, self.world.notes())
        services.add(ListOrders, ListOrders(self.queries))
        services.add(GetOrder, GetOrder(self.queries))
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


# Every route with a valid body (the order it acts on is stored by the test).
ROUTES: list[tuple[str, str, dict[str, Any] | None, str]] = [
    ("GET", "", None, "orders:manage"),
    ("GET", "/LFI-1001", None, "orders:manage"),
    ("POST", "/LFI-1001/payment", {"note": "Transferencia"}, "orders:finance"),
    ("POST", "/LFI-1001/advance", {"to": "preparing"}, "orders:manage"),
    ("POST", "/LFI-1001/cancel", {"reason": "Motivo"}, "orders:manage"),
    ("POST", "/LFI-1001/cancel-paid", {"reason": "Motivo"}, "orders:finance"),
    ("POST", "/LFI-1001/refund", {"note": "Hecho"}, "orders:finance"),
    ("POST", "/LFI-1001/notes", {"text": "Hola"}, "orders:manage"),
]


# --- access ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body", "permission"), ROUTES)
async def test_every_route_answers_401_without_a_session(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None, permission: str
) -> None:
    response = await client.request(method, BASE + path, json=body)

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize(("method", "path", "body", "permission"), ROUTES)
async def test_every_route_answers_401_for_an_unknown_session(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None, permission: str
) -> None:
    headers = {"Cookie": f"{SESSION_COOKIE}=nobody"}

    response = await client.request(method, BASE + path, json=body, headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body", "permission"), ROUTES)
async def test_every_route_answers_403_without_any_orders_permission(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None, permission: str
) -> None:
    response = await client.request(method, BASE + path, json=body, headers=CATALOG_ONLY)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    ("method", "path", "body", "permission"), [r for r in ROUTES if r[3] == "orders:finance"]
)
async def test_staff_gets_403_on_the_finance_routes(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None, permission: str
) -> None:
    response = await client.request(method, BASE + path, json=body, headers=STAFF)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    ("method", "path", "body", "permission"), [r for r in ROUTES if r[3] == "orders:finance"]
)
async def test_the_finance_permission_alone_is_not_enough_for_the_finance_routes(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None, permission: str
) -> None:
    """The router requires `orders:manage` and the route adds `orders:finance`: both."""
    response = await client.request(method, BASE + path, json=body, headers=FINANCE_ONLY)

    assert response.status_code == 403


async def test_a_forbidden_payment_changes_nothing(client: AsyncClient, harness: Harness) -> None:
    await harness.world.store()

    await client.post(f"{BASE}/LFI-1001/payment", json={"note": "x"}, headers=STAFF)

    assert harness.world.current().status is S.PENDING_PAYMENT
    assert harness.world.stocks.operations == []


@pytest.mark.parametrize(
    ("method", "path", "body", "permission"), [r for r in ROUTES if r[3] == "orders:manage"]
)
async def test_staff_may_use_the_manage_routes(
    client: AsyncClient,
    harness: Harness,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    permission: str,
) -> None:
    await harness.world.store(S.PAID if "advance" in path else S.PENDING_PAYMENT)

    response = await client.request(method, BASE + path, json=body, headers=STAFF)

    assert response.status_code in (200, 201, 204)


# --- list and detail ---------------------------------------------------------------------------


async def test_the_list_passes_the_filters_and_returns_the_page(
    client: AsyncClient, harness: Harness
) -> None:
    response = await client.get(
        BASE, params={"status": "paid", "q": "  ana ", "page": 2, "size": 5}, headers=STAFF
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["page"], body["size"]) == (1, 2, 5)
    assert body["items"][0]["number"] == "LFI-1001"
    assert harness.queries.page_calls == [("paid", "ana", 2, 5)]


async def test_the_list_defaults_to_the_first_page_without_filters(
    client: AsyncClient, harness: Harness
) -> None:
    await client.get(BASE, headers=STAFF)

    status, q, page, size = harness.queries.page_calls[0]
    assert (status, q, page) == (None, None, 1)
    assert size == 20


async def test_a_blank_search_is_ignored(client: AsyncClient, harness: Harness) -> None:
    await client.get(BASE, params={"q": "   "}, headers=STAFF)

    assert harness.queries.page_calls[0][1] is None


@pytest.mark.parametrize(
    "params",
    [
        {"status": "nope"},
        {"page": 0},
        {"page": 10_001},
        {"size": 0},
        {"size": 101},
        {"q": "x" * 101},
    ],
)
async def test_the_list_validates_its_query(client: AsyncClient, params: dict[str, Any]) -> None:
    response = await client.get(BASE, params=params, headers=STAFF)

    assert response.status_code == 422


@pytest.mark.parametrize("status", [s.value for s in OrderStatus])
async def test_the_list_accepts_each_of_the_seven_statuses(
    client: AsyncClient, status: str
) -> None:
    assert (await client.get(BASE, params={"status": status}, headers=STAFF)).status_code == 200


@pytest.mark.parametrize("path", ["LFI-1001", "1001", "lfi-1001"])
async def test_the_detail_accepts_the_number_with_or_without_the_prefix(
    client: AsyncClient, harness: Harness, path: str
) -> None:
    response = await client.get(f"{BASE}/{path}", headers=STAFF)

    assert response.status_code == 200
    assert harness.queries.detail_calls == [1001]
    body = response.json()
    assert body["number"] == "LFI-1001"
    assert body["history"][0]["to_status"] == "paid"
    assert body["notes"][0]["text"] == "Hola"
    assert body["reservation_id"] is None


@pytest.mark.parametrize("path", ["abc", "LFI-", "-1", "1.5", "LFI1001", "1234567890"])
async def test_a_number_that_does_not_parse_is_a_404_and_never_reaches_the_query(
    client: AsyncClient, harness: Harness, path: str
) -> None:
    response = await client.get(f"{BASE}/{path}", headers=STAFF)

    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_ORDER_NOT_FOUND"
    assert harness.queries.detail_calls == []


async def test_an_unknown_but_well_formed_number_is_a_404(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/LFI-9999", headers=STAFF)

    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_ORDER_NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/abc/payment", {"note": "x"}),
        ("POST", "/abc/advance", {"to": "delivered"}),
        ("POST", "/abc/cancel", {"reason": "x"}),
        ("POST", "/abc/cancel-paid", {"reason": "x"}),
        ("POST", "/abc/refund", {"note": "x"}),
        ("POST", "/abc/notes", {"text": "x"}),
    ],
)
async def test_every_write_route_answers_404_for_a_number_that_does_not_parse(
    client: AsyncClient, method: str, path: str, body: dict[str, Any]
) -> None:
    response = await client.request(method, BASE + path, json=body, headers=OWNER)

    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_ORDER_NOT_FOUND"


# --- paid by hand ------------------------------------------------------------------------------


async def test_the_owner_marks_an_order_paid_with_204_and_the_actor_is_the_session_user(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store()

    response = await client.post(
        f"{BASE}/1001/payment", json={"note": "Transferencia directa"}, headers=OWNER
    )

    assert response.status_code == 204
    assert response.content == b""
    assert harness.world.current().status is S.PAID
    assert harness.world.stocks.operations == [("commit", UUID(int=71))]
    assert harness.world.history() == [(S.PENDING_PAYMENT, S.PAID, "owner-session")]


async def test_paying_twice_is_a_409_with_from_and_to(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.PAID)

    response = await client.post(f"{BASE}/LFI-1001/payment", json={"note": "x"}, headers=OWNER)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "ORDERS_TRANSITION_INVALID"
    assert body["details"] == {"from": "paid", "to": "paid"}


async def test_an_inventory_refusal_is_a_409_with_its_own_code_and_leaves_the_order_unpaid(
    client: AsyncClient, harness: Harness
) -> None:
    harness.world.stocks.refusal = refusal("INVENTORY_STOCK_UNAVAILABLE")
    await harness.world.store()

    response = await client.post(f"{BASE}/1001/payment", json={"note": "x"}, headers=OWNER)

    assert response.status_code == 409
    assert response.json()["code"] == "INVENTORY_STOCK_UNAVAILABLE"
    assert harness.world.current().status is S.PENDING_PAYMENT


@pytest.mark.parametrize("note", ["", "   "])
async def test_a_blank_payment_note_is_a_422_naming_the_field(
    client: AsyncClient, harness: Harness, note: str
) -> None:
    await harness.world.store()

    response = await client.post(f"{BASE}/1001/payment", json={"note": note}, headers=OWNER)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ORDERS_ORDER_DATA_INVALID"
    assert body["details"]["field"] == "note"


async def test_a_payment_without_a_note_is_a_422(client: AsyncClient) -> None:
    response = await client.post(f"{BASE}/1001/payment", json={}, headers=OWNER)

    assert response.status_code == 422


async def test_an_oversized_payment_note_is_a_422_at_the_boundary_of_the_contract(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store()

    over_domain = await client.post(f"{BASE}/1001/payment", json={"note": "n" * 301}, headers=OWNER)
    over_contract = await client.post(
        f"{BASE}/1001/payment", json={"note": "n" * 1001}, headers=OWNER
    )

    assert over_domain.status_code == 422
    assert over_domain.json()["code"] == "ORDERS_ORDER_DATA_INVALID"
    assert over_contract.status_code == 422


async def test_paying_an_unknown_order_is_a_404(client: AsyncClient) -> None:
    response = await client.post(f"{BASE}/LFI-9999/payment", json={"note": "x"}, headers=OWNER)

    assert response.status_code == 404
    assert response.json()["code"] == "ORDERS_ORDER_NOT_FOUND"


# --- advance -----------------------------------------------------------------------------------


async def test_a_home_order_ships_with_carrier_and_tracking(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.PAID)

    response = await client.post(
        f"{BASE}/1001/advance",
        json={"to": "shipped", "carrier": "Estafeta", "tracking_number": "123456"},
        headers=STAFF,
    )

    assert response.status_code == 204
    stored = harness.world.current()
    assert (stored.status, stored.shipping_carrier, stored.tracking_number) == (
        S.SHIPPED,
        "Estafeta",
        "123456",
    )
    assert harness.world.history() == [(S.PAID, S.SHIPPED, "staff-session")]


async def test_shipping_a_home_order_without_data_is_a_422(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.PAID)

    response = await client.post(f"{BASE}/1001/advance", json={"to": "shipped"}, headers=STAFF)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ORDERS_ORDER_DATA_INVALID"
    assert body["details"]["field"] == "carrier"


async def test_a_pickup_order_goes_through_preparing_ready_and_delivered(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.PAID, method=DeliveryMethod.PICKUP)

    for target in ("preparing", "ready_for_pickup", "delivered"):
        response = await client.post(f"{BASE}/1001/advance", json={"to": target}, headers=STAFF)
        assert response.status_code == 204, target

    assert harness.world.current().status is S.DELIVERED


async def test_shipping_a_pickup_order_is_a_409(client: AsyncClient, harness: Harness) -> None:
    await harness.world.store(S.PAID, method=DeliveryMethod.PICKUP)

    response = await client.post(f"{BASE}/1001/advance", json={"to": "shipped"}, headers=STAFF)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "ORDERS_TRANSITION_INVALID"
    assert body["details"] == {"from": "paid", "to": "shipped"}


async def test_moving_a_delivered_order_back_is_a_409(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.DELIVERED)

    response = await client.post(f"{BASE}/1001/advance", json={"to": "preparing"}, headers=STAFF)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_TRANSITION_INVALID"


@pytest.mark.parametrize("target", ["paid", "cancelled", "pending_payment", "", "teleported"])
async def test_the_advance_targets_are_limited_to_the_four_forward_moves(
    client: AsyncClient, harness: Harness, target: str
) -> None:
    await harness.world.store(S.PAID)

    response = await client.post(f"{BASE}/1001/advance", json={"to": target}, headers=STAFF)

    assert response.status_code == 422
    assert harness.world.current().status is S.PAID


async def test_advancing_an_unknown_order_is_a_404(client: AsyncClient) -> None:
    response = await client.post(f"{BASE}/9999/advance", json={"to": "delivered"}, headers=STAFF)

    assert response.status_code == 404


# --- cancel ------------------------------------------------------------------------------------


async def test_staff_cancels_an_unpaid_order_and_the_reservation_is_released(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store()

    response = await client.post(
        f"{BASE}/LFI-1001/cancel", json={"reason": "El cliente lo pidió"}, headers=STAFF
    )

    assert response.status_code == 204
    stored = harness.world.current()
    assert (stored.status, stored.cancel_reason) == (S.CANCELLED, "El cliente lo pidió")
    assert harness.world.stocks.operations == [("release", UUID(int=71))]


async def test_cancel_on_a_paid_order_is_a_409(client: AsyncClient, harness: Harness) -> None:
    await harness.world.store(S.PAID)

    response = await client.post(f"{BASE}/1001/cancel", json={"reason": "x"}, headers=STAFF)

    assert response.status_code == 409
    assert response.json()["code"] == "ORDERS_TRANSITION_INVALID"
    assert harness.world.stocks.operations == []


async def test_cancel_without_a_reason_is_a_422(client: AsyncClient, harness: Harness) -> None:
    await harness.world.store()

    blank = await client.post(f"{BASE}/1001/cancel", json={"reason": " "}, headers=STAFF)
    missing = await client.post(f"{BASE}/1001/cancel", json={}, headers=STAFF)

    assert blank.status_code == 422
    assert blank.json()["details"]["field"] == "reason"
    assert missing.status_code == 422


async def test_the_owner_cancels_a_paid_order_and_the_refund_is_pending(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.PAID)

    response = await client.post(f"{BASE}/1001/cancel-paid", json={"reason": "x"}, headers=OWNER)

    assert response.status_code == 204
    stored = harness.world.current()
    assert (stored.status, stored.refund_status.value) == (S.CANCELLED, "pending")  # type: ignore[union-attr]
    assert harness.world.stocks.operations == [("return_sold", UUID(int=71))]


async def test_cancel_paid_on_an_unpaid_order_is_a_409(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store()

    response = await client.post(f"{BASE}/1001/cancel-paid", json={"reason": "x"}, headers=OWNER)

    assert response.status_code == 409
    assert response.json()["details"] == {"from": "pending_payment", "to": "cancelled"}


# --- refund ------------------------------------------------------------------------------------


async def test_the_owner_marks_the_refund_done_and_a_second_call_is_a_409(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store(S.PAID)
    await client.post(f"{BASE}/1001/cancel-paid", json={"reason": "x"}, headers=OWNER)

    first = await client.post(f"{BASE}/1001/refund", json={"note": "Reembolso"}, headers=OWNER)
    second = await client.post(f"{BASE}/1001/refund", json={"note": "Otra vez"}, headers=OWNER)

    assert first.status_code == 204
    assert harness.world.current().refund_status.value == "done"  # type: ignore[union-attr]
    assert second.status_code == 409
    assert second.json()["code"] == "ORDERS_TRANSITION_INVALID"


async def test_a_refund_without_a_note_is_a_422(client: AsyncClient, harness: Harness) -> None:
    await harness.world.store(S.PAID)
    await client.post(f"{BASE}/1001/cancel-paid", json={"reason": "x"}, headers=OWNER)

    response = await client.post(f"{BASE}/1001/refund", json={"note": " "}, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["details"]["field"] == "note"


# --- notes -------------------------------------------------------------------------------------


async def test_a_note_answers_201_with_no_body_and_is_signed_by_the_session_user(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store()

    response = await client.post(f"{BASE}/1001/notes", json={"text": "Hola"}, headers=STAFF)

    assert response.status_code == 201
    assert response.content in (b"", b"null")
    assert [(a, t) for _, a, t in harness.world.orders.notes[UUID(int=901)]] == [
        ("staff-session", "Hola")
    ]


async def test_a_note_on_an_unknown_order_is_a_404(client: AsyncClient) -> None:
    response = await client.post(f"{BASE}/LFI-9999/notes", json={"text": "Hola"}, headers=STAFF)

    assert response.status_code == 404


@pytest.mark.parametrize("text", ["", "  ", "x" * 1001])
async def test_a_blank_or_long_note_is_a_422(
    client: AsyncClient, harness: Harness, text: str
) -> None:
    await harness.world.store()

    response = await client.post(f"{BASE}/1001/notes", json={"text": text}, headers=STAFF)

    assert response.status_code == 422
    assert response.json()["code"] == "ORDERS_ORDER_DATA_INVALID"
    assert harness.world.orders.notes == {}


async def test_a_note_over_the_contract_bound_is_a_422(
    client: AsyncClient, harness: Harness
) -> None:
    await harness.world.store()

    response = await client.post(f"{BASE}/1001/notes", json={"text": "x" * 5001}, headers=STAFF)

    assert response.status_code == 422


# --- OpenAPI -----------------------------------------------------------------------------------


def test_the_order_operations_are_in_the_admin_openapi(app: FastAPI) -> None:
    operations = admin_operations(app)

    for method, path in [
        ("GET", BASE),
        ("GET", f"{BASE}/{{number}}"),
        ("POST", f"{BASE}/{{number}}/payment"),
        ("POST", f"{BASE}/{{number}}/advance"),
        ("POST", f"{BASE}/{{number}}/cancel"),
        ("POST", f"{BASE}/{{number}}/cancel-paid"),
        ("POST", f"{BASE}/{{number}}/refund"),
        ("POST", f"{BASE}/{{number}}/notes"),
    ]:
        assert (method, path) in operations


def test_the_openapi_declares_the_success_status_of_each_write_route(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    expected = {
        "payment": "204",
        "advance": "204",
        "cancel": "204",
        "cancel-paid": "204",
        "refund": "204",
        "notes": "201",
    }
    for route, status in expected.items():
        responses = paths[f"{BASE}/{{number}}/{route}"]["post"]["responses"]
        assert status in responses, route
        assert {"404", "422"} <= responses.keys(), route
    for route in ("payment", "advance", "cancel", "cancel-paid", "refund"):
        assert "409" in paths[f"{BASE}/{{number}}/{route}"]["post"]["responses"], route


def test_the_openapi_describes_the_order_models(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert {
        "AdminOrderPage",
        "AdminOrderDetail",
        "AdminOrderSummary",
        "AdminOrderEvent",
        "AdminOrderNote",
        "AdminOrderLine",
        "MarkPaidRequest",
        "AdvanceOrderRequest",
        "CancelOrderRequest",
        "RefundDoneRequest",
        "OrderNoteRequest",
    } <= schemas.keys()
    assert schemas["AdvanceOrderRequest"]["properties"]["to"]["enum"] == [
        "preparing",
        "shipped",
        "ready_for_pickup",
        "delivered",
    ]
