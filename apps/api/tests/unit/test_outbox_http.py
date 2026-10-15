"""Plan platform-api-foundation/004: the owner-only failed-events routes
(`shared/http/outbox.py`) over an in-memory `OutboxAdmin`."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.identity.domain.user import OUTBOX_MANAGE, ROLE_PERMISSIONS, Role
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.application.events import OutboxAdmin
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from fragancia_api.shared.contracts.outbox import FailedEvent, FailedEventPage
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http import outbox as outbox_http
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import assert_admin_routes_are_protected

OWNER = {"Cookie": f"{SESSION_COOKIE}=owner-session"}
STAFF = {"Cookie": f"{SESSION_COOKIE}=staff-session"}
FAILED_ID = UUID(int=7)
UNKNOWN_ID = UUID(int=8)
AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
BASE = "/api/v1/admin/outbox"


class RoleResolver:
    """An owner and a staff member with the permissions identity really gives each role."""

    async def resolve(self, token: str) -> Actor | None:
        roles = {"owner-session": Role.OWNER, "staff-session": Role.STAFF}
        if token not in roles:
            return None
        return Actor(
            id=token,
            is_admin=True,
            session_id=UUID(int=1),
            permissions=ROLE_PERMISSIONS[roles[token]],
        )


@dataclass
class FakeOutboxAdmin:
    failed_ids: set[UUID] = field(default_factory=lambda: {FAILED_ID})
    list_calls: list[tuple[int, int]] = field(default_factory=list)
    retried: list[UUID] = field(default_factory=list)

    async def failed(self, *, page: int, size: int) -> FailedEventPage:
        self.list_calls.append((page, size))
        item = FailedEvent(
            id=FAILED_ID,
            name="tests.something.happened",
            occurred_at=AT,
            failed_at=AT,
            attempts=295,
            last_error="ConnectionError('smtp down')",
        )
        return FailedEventPage(items=[item], total=1, page=page, size=size)

    async def retry(self, event_id: UUID) -> bool:
        self.retried.append(event_id)
        return event_id in self.failed_ids


@pytest.fixture
def admin() -> FakeOutboxAdmin:
    return FakeOutboxAdmin()


@pytest.fixture
async def client(admin: FakeOutboxAdmin) -> AsyncIterator[AsyncClient]:
    async def ok() -> None:
        return None

    services = ServiceRegistry()
    services.add(ActorResolver, RoleResolver())  # type: ignore[type-abstract]
    services.add(OutboxAdmin, admin)  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks(database=ok, valkey=ok))
    app = build_app(services, [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def test_the_router_permission_literal_is_the_one_identity_grants_to_owners() -> None:
    assert outbox_http.OUTBOX_MANAGE == OUTBOX_MANAGE == "outbox:manage"


async def test_an_owner_lists_failed_events(client: AsyncClient, admin: FakeOutboxAdmin) -> None:
    response = await client.get(f"{BASE}/failed", headers=OWNER)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": str(FAILED_ID),
                "name": "tests.something.happened",
                "occurred_at": "2026-10-15T12:00:00Z",
                "failed_at": "2026-10-15T12:00:00Z",
                "attempts": 295,
                "last_error": "ConnectionError('smtp down')",
            }
        ],
        "total": 1,
        "page": 1,
        "size": DEFAULT_PAGE_SIZE,
    }
    assert admin.list_calls == [(1, DEFAULT_PAGE_SIZE)]


async def test_paging_parameters_reach_the_port(
    client: AsyncClient, admin: FakeOutboxAdmin
) -> None:
    response = await client.get(f"{BASE}/failed?page=3&size={MAX_PAGE_SIZE}", headers=OWNER)

    assert response.status_code == 200
    assert admin.list_calls == [(3, MAX_PAGE_SIZE)]


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        f"page={MAX_PAGE + 1}",
        "size=0",
        f"size={MAX_PAGE_SIZE + 1}",
        "page=abc",
    ],
)
async def test_out_of_bounds_paging_is_a_validation_error(
    client: AsyncClient, admin: FakeOutboxAdmin, query: str
) -> None:
    response = await client.get(f"{BASE}/failed?{query}", headers=OWNER)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert admin.list_calls == []


async def test_an_owner_retries_a_failed_event(client: AsyncClient, admin: FakeOutboxAdmin) -> None:
    response = await client.post(f"{BASE}/{FAILED_ID}/retry", headers=OWNER)

    assert response.status_code == 204
    assert response.content == b""
    assert admin.retried == [FAILED_ID]


async def test_retrying_an_unknown_or_not_failed_event_is_404(client: AsyncClient) -> None:
    response = await client.post(f"{BASE}/{UNKNOWN_ID}/retry", headers=OWNER)

    assert response.status_code == 404
    assert response.json() == {
        "code": "OUTBOX_EVENT_NOT_FOUND",
        "message": "No failed event with this id",
    }


async def test_a_malformed_event_id_is_a_validation_error(
    client: AsyncClient, admin: FakeOutboxAdmin
) -> None:
    response = await client.post(f"{BASE}/not-a-uuid/retry", headers=OWNER)

    assert response.status_code == 422
    assert admin.retried == []


async def test_staff_is_forbidden_on_both_routes(
    client: AsyncClient, admin: FakeOutboxAdmin
) -> None:
    listing = await client.get(f"{BASE}/failed", headers=STAFF)
    retry = await client.post(f"{BASE}/{FAILED_ID}/retry", headers=STAFF)

    assert (listing.status_code, listing.json()["code"]) == (403, "FORBIDDEN")
    assert (retry.status_code, retry.json()["code"]) == (403, "FORBIDDEN")
    assert admin.list_calls == [] and admin.retried == []


async def test_unauthenticated_requests_are_401_on_both_routes(
    client: AsyncClient, admin: FakeOutboxAdmin
) -> None:
    listing = await client.get(f"{BASE}/failed")
    retry = await client.post(f"{BASE}/{FAILED_ID}/retry")
    unknown = await client.get(f"{BASE}/failed", headers={"Cookie": f"{SESSION_COOKIE}=nobody"})

    for response in (listing, retry, unknown):
        assert (response.status_code, response.json()["code"]) == (401, "AUTHENTICATION_REQUIRED")
    assert admin.list_calls == [] and admin.retried == []


async def test_the_openapi_document_describes_both_protected_operations(
    client: AsyncClient,
) -> None:
    document = (await client.get("/api/v1/openapi.json")).json()
    paths = document["paths"]

    listing = paths["/api/v1/admin/outbox/failed"]["get"]
    retry = paths["/api/v1/admin/outbox/{event_id}/retry"]["post"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/FailedEventPage"
    }
    assert set(retry["responses"]) >= {"204", "401", "403", "404"}
    assert {"APIKeyCookie": []} in listing["security"]
    assert {"APIKeyCookie": []} in retry["security"]
    assert set(document["components"]["schemas"]["FailedEvent"]["properties"]) == {
        "id",
        "name",
        "occurred_at",
        "failed_at",
        "attempts",
        "last_error",
    }


async def test_the_outbox_routes_declare_admin_access(client: AsyncClient) -> None:
    services = ServiceRegistry()
    services.add(ActorResolver, RoleResolver())  # type: ignore[type-abstract]
    assert_admin_routes_are_protected(build_app(services, []))
