"""Plan 003 step 5: `GetPendingTickets` (application) and `POST /api/v1/payments/pending` (http):
which payments are listed, the order they come in, an unknown token, an order that is not waiting
for payment, the token only in the body, the labels of the contract and the OpenAPI document."""

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.payments.application.queries.pending_tickets import GetPendingTickets
from fragancia_api.modules.payments.contracts import PendingTicket
from fragancia_api.modules.payments.domain.errors import PaymentOrderNotFound
from fragancia_api.modules.payments.domain.payment import Payment
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.modules.payments.infrastructure.in_memory import (
    InMemoryPayableOrders,
    InMemoryPayments,
)
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Err, Ok
from tests.support import TestActorResolver
from tests.unit.payments.support import NOW, ORDER_ID, TOKEN, make_payable

PENDING = "/api/v1/payments/pending"


def payment(payment_id: str, **overrides: object) -> Payment:
    values: dict[str, object] = {
        "payment_id": payment_id,
        "order_id": ORDER_ID,
        "status": "pending",
        "status_detail": "pending_waiting_payment",
        "amount_cents": 268_000,
        "currency": "MXN",
        "payment_type": "ticket",
        "approved_at": None,
        "outcome": None,
        "created_at": NOW,
        "updated_at": NOW,
        "payment_method": "oxxo",
        "expires_at": NOW + timedelta(hours=48),
        "ticket_url": f"https://mp.test/ticket/{payment_id}",
    }
    values.update(overrides)
    return Payment(**values)  # type: ignore[arg-type]


def query(*payments: Payment, status: str = "pending_payment") -> GetPendingTickets:
    orders = InMemoryPayableOrders()
    # A held order's deadline is its ticket's expiry; the query caps each expiry at it (R1).
    orders.by_token[TOKEN] = make_payable(status=status, due=timedelta(hours=48))
    return GetPendingTickets(
        orders=orders, queries=InMemoryPayments(*payments), clock=FixedClock(NOW)
    )


async def listed(use_case: GetPendingTickets, token: str = TOKEN) -> list[PendingTicket]:
    result = await use_case.execute(token)
    assert isinstance(result, Ok), result
    return result.value.items


# --- application -------------------------------------------------------------------------------


async def test_a_live_oxxo_ticket_is_listed_with_its_label_amount_expiry_and_page() -> None:
    items = await listed(query(payment("1")))

    assert items == [
        PendingTicket(
            method="efectivo en tienda",
            amount_cents=268_000,
            expires_at=NOW + timedelta(hours=48),
            ticket_url="https://mp.test/ticket/1",
        )
    ]


async def test_a_spei_transfer_is_listed_with_its_own_label() -> None:
    items = await listed(query(payment("1", payment_type="bank_transfer", payment_method="clabe")))

    assert [i.method for i in items] == ["transferencia SPEI"]


async def test_a_ticket_without_a_page_is_listed_with_none() -> None:
    items = await listed(query(payment("1", ticket_url=None)))

    assert [i.ticket_url for i in items] == [None]


async def test_the_tickets_come_newest_first() -> None:
    items = await listed(
        query(
            payment("old", created_at=NOW - timedelta(hours=2), ticket_url="u-old"),
            payment("new", created_at=NOW, ticket_url="u-new"),
            payment("mid", created_at=NOW - timedelta(hours=1), ticket_url="u-mid"),
        )
    )

    assert [i.ticket_url for i in items] == ["u-new", "u-mid", "u-old"]


async def test_an_expired_ticket_is_hidden_and_one_expiring_exactly_now_too() -> None:
    items = await listed(
        query(
            payment("past", expires_at=NOW - timedelta(seconds=1)),
            payment("edge", expires_at=NOW),
            payment("live", expires_at=NOW + timedelta(seconds=1), ticket_url="u-live"),
        )
    )

    assert [i.ticket_url for i in items] == ["u-live"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "cancelled"},
        {"status": "expired"},
        {"payment_type": "credit_card"},
        {"payment_type": "atm"},
        {"payment_type": None},
        {"expires_at": None},
        {"order_id": UUID(int=1)},
        {"order_id": None},
    ],
)
async def test_other_payments_are_not_listed(overrides: dict[str, object]) -> None:
    assert await listed(query(payment("1", **overrides))) == []


async def test_a_ticket_already_held_is_still_listed() -> None:
    assert len(await listed(query(payment("1", held_at=NOW)))) == 1


async def test_an_unknown_token_is_order_not_found() -> None:
    result = await query(payment("1")).execute("nope")

    assert isinstance(result, Err)
    assert isinstance(result.error, PaymentOrderNotFound)


@pytest.mark.parametrize("status", ["paid", "preparing", "shipped", "delivered", "cancelled"])
async def test_an_order_that_is_not_pending_payment_has_no_tickets_even_with_a_live_one(
    status: str,
) -> None:
    assert await listed(query(payment("1"), status=status)) == []


# --- http --------------------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(GetPendingTickets, query(payment("1"), payment("2", payment_type="bank_transfer")))
    app = build_app(services, routers)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_a_known_token_answers_200_with_the_items(client: AsyncClient) -> None:
    response = await client.post(PENDING, json={"access_token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items"}
    # Created at the same instant: the higher payment id comes first.
    assert [item["method"] for item in body["items"]] == [
        "transferencia SPEI",
        "efectivo en tienda",
    ]
    first = body["items"][0]
    assert set(first) == {"method", "amount_cents", "expires_at", "ticket_url"}
    assert first["amount_cents"] == 268_000
    assert first["expires_at"] == "2026-10-24T12:00:00Z"


async def test_an_order_with_no_live_ticket_answers_200_with_no_items() -> None:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(GetPendingTickets, query(status="paid"))
    async with AsyncClient(
        transport=ASGITransport(app=build_app(services, routers)), base_url="http://t"
    ) as c:
        response = await c.post(PENDING, json={"access_token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {"items": []}


async def test_an_unknown_token_is_404(client: AsyncClient) -> None:
    response = await client.post(PENDING, json={"access_token": "nope"})

    assert response.status_code == 404
    assert response.json()["code"] == "PAYMENTS_ORDER_NOT_FOUND"


@pytest.mark.parametrize("body", [{}, {"access_token": 5}, {"access_token": "x" * 201}, None])
async def test_an_invalid_body_is_422(client: AsyncClient, body: object) -> None:
    response = await client.post(PENDING, json=body)

    assert response.status_code == 422


async def test_the_token_is_not_accepted_in_the_url(client: AsyncClient) -> None:
    by_query = await client.post(f"{PENDING}?access_token={TOKEN}")
    by_path = await client.post(f"{PENDING}/{TOKEN}")
    by_get = await client.get(PENDING)

    assert by_query.status_code == 422
    assert by_path.status_code == 404
    assert by_get.status_code == 405


async def test_the_route_is_public(client: AsyncClient) -> None:
    response = await client.post(PENDING, json={"access_token": TOKEN})

    assert response.status_code == 200  # no session cookie sent


async def test_the_openapi_document_describes_the_route_with_its_body_and_statuses() -> None:
    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    schema = build_app(services, routers).openapi()

    operation = schema["paths"][PENDING]["post"]
    assert set(schema["paths"][PENDING]) == {"post"}
    assert "parameters" not in operation
    assert {"200", "404", "422"} <= set(operation["responses"])
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/PendingTicketsRequest"}
    request = schema["components"]["schemas"]["PendingTicketsRequest"]
    assert request["required"] == ["access_token"]
    assert request["properties"]["access_token"]["maxLength"] == 200
    ticket = schema["components"]["schemas"]["PendingTicket"]
    assert set(ticket["required"]) == {"method", "amount_cents", "expires_at", "ticket_url"}
    assert operation["tags"] == ["payments"]
