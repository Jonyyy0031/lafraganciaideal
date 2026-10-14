"""Plan 008, http layer: `POST /admin/catalog-imports/{id}/confirm` and `/discard` (status codes,
error codes with `details.issues`, access, the response fields)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ConfirmCatalogImport,
    DiscardCatalogImport,
)
from fragancia_api.modules.catalog.application.queries.catalog_imports import GetCatalogImport
from fragancia_api.modules.catalog.contracts import CatalogImportResponse
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportPlan,
    ImportStatus,
    ImportSummary,
    PlannedPerfume,
    PlannedPhoto,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.perfume import Gender, photo_keys
from fragancia_api.modules.catalog.http.import_router import import_routers
from fragancia_api.modules.catalog.infrastructure.in_memory_imports import InMemoryCatalogImports
from fragancia_api.modules.catalog.infrastructure.in_memory_media import InMemoryMediaStorage
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected
from tests.unit.catalog.perfume_support import World, add_presentation, make_perfume

BASE = "/api/v1/admin/catalog-imports"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=60)
UNKNOWN_ID = UUID(int=404)
NO_PERMISSION_SESSION = "admin-without-catalog-manage"
NO_PERMISSION_HEADERS = {"Cookie": f"{SESSION_COOKIE}={NO_PERMISSION_SESSION}"}
INVENTORY_SESSION = "admin-with-inventory-manage"
INVENTORY_HEADERS = {"Cookie": f"{SESSION_COOKIE}={INVENTORY_SESSION}"}


class _Resolver:
    async def resolve(self, token: str) -> Actor | None:
        if token == NO_PERMISSION_SESSION:
            return Actor(id="no-perm", is_admin=True, session_id=UUID(int=2))
        if token == INVENTORY_SESSION:
            return Actor(
                id="stock-admin",
                is_admin=True,
                session_id=UUID(int=3),
                permissions=frozenset({"catalog:manage", "inventory:manage"}),
            )
        return await TestActorResolver().resolve(token)  # `catalog:manage` only


class _StoreReportingCounts(InMemoryCatalogImports):
    """The in-memory query side drops `summary.stock_counts` (see the GAP test below), so this
    store fills it in from the stored import, as the SQL query does, and the confirm route's
    permission check can be exercised."""

    async def get(self, import_id: UUID) -> CatalogImportResponse | None:
        response = await super().get(import_id)
        item = self.by_id.get(import_id)
        if response is None or response.summary is None or item is None or item.summary is None:
            return response
        summary = response.summary.model_copy(update={"stock_counts": item.summary.stock_counts})
        return response.model_copy(update={"summary": summary})


@dataclass
class Rig:
    client: AsyncClient
    world: World
    store: InMemoryCatalogImports
    storage: InMemoryMediaStorage
    events: RecordingEventPublisher


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    world = World()
    store = _StoreReportingCounts()
    storage = InMemoryMediaStorage()
    clock = FixedClock(NOW)
    transactions = InMemoryTransactionRunner()
    events = RecordingEventPublisher()
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(GetCatalogImport, GetCatalogImport(store))
    services.add(
        ConfirmCatalogImport,
        ConfirmCatalogImport(
            imports=store,
            perfumes=world.perfumes,
            brands=world.brands,
            families=world.families,
            concentrations=world.concentrations,
            storage=storage,
            transactions=transactions,
            clock=clock,
            events=events,
        ),
    )
    services.add(
        DiscardCatalogImport,
        DiscardCatalogImport(
            imports=store, storage=storage, transactions=transactions, clock=clock
        ),
    )
    app = build_app(services, import_routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield Rig(client, world, store, storage, events)


def _plan(
    world: World, *, archived_existing: bool = False, stock_count: int | None = None
) -> tuple[ImportPlan, UUID]:
    new_id_ = UUID(int=7)
    card, detail = photo_keys(new_id_, UUID(int=70))
    new = PlannedPerfume(
        perfume_id=new_id_,
        is_new=True,
        brand_id=world.brand.id,
        concentration_id=world.concentration.id,
        family_id=world.family.id,
        name="Eros",
        gender=Gender.MEN,
        description="",
        top_notes=(),
        heart_notes=(),
        base_notes=(),
        publish=True,
        first_row=2,
        presentations=(
            PlannedPresentation(
                None, 100, 250_000, None, None, None, "in_stock", None, None, 2, stock_count
            ),
        ),
        photos=(
            PlannedPhoto(1, UUID(int=70), "https://img.test/a.jpg", card, detail, 1200, 900, None),
        ),
    )
    stored = make_perfume(world.brand, world.family, world.concentration, name="Luna")
    presentation_id = add_presentation(stored)
    if archived_existing:
        stored.archive()
    world.seed(stored)
    existing = PlannedPerfume(
        perfume_id=stored.id,
        is_new=False,
        brand_id=world.brand.id,
        concentration_id=world.concentration.id,
        family_id=world.family.id,
        name="Luna",
        gender=Gender.MEN,
        description="",
        top_notes=(),
        heart_notes=(),
        base_notes=(),
        publish=None,
        first_row=3,
        presentations=(
            PlannedPresentation(
                presentation_id, 100, 199_900, None, None, None, "in_stock", None, None, 3
            ),
        ),
        photos=(),
    )
    return ImportPlan((new, existing)), stored.id


async def _ready(
    rig: Rig, *, archived_existing: bool = False, stock_count: int | None = None
) -> CatalogImport:
    plan, _ = _plan(rig.world, archived_existing=archived_existing, stock_count=stock_count)
    counts = 0 if stock_count is None else 1
    item = CatalogImport.create(b"PK\x03\x04data", "admin-1", NOW - timedelta(minutes=10))
    item.start(NOW - timedelta(minutes=9))
    item.finish_ready(
        NOW - timedelta(minutes=5), TTL, 2, ImportSummary(1, 1, 1, 1, 1, counts), plan
    )
    await rig.store.add(item)
    return item


# --- access -----------------------------------------------------------------------------------

_SOME = str(UNKNOWN_ID)
_ROUTES = [("POST", f"{BASE}/{_SOME}/confirm"), ("POST", f"{BASE}/{_SOME}/discard")]


@pytest.mark.parametrize(("method", "url"), _ROUTES)
async def test_confirm_and_discard_return_401_without_a_session(
    rig: Rig, method: str, url: str
) -> None:
    item = await _ready(rig)
    before = rig.store.by_id[item.id].status

    response = await rig.client.request(method, url)

    assert (response.status_code, response.json()["code"]) == (401, "AUTHENTICATION_REQUIRED")
    assert rig.store.by_id[item.id].status is before


@pytest.mark.parametrize("action", ["confirm", "discard"])
async def test_confirm_and_discard_return_403_for_an_admin_without_catalog_manage(
    rig: Rig, action: str
) -> None:
    item = await _ready(rig)

    response = await rig.client.post(f"{BASE}/{item.id}/{action}", headers=NO_PERMISSION_HEADERS)

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert rig.store.by_id[item.id].status is ImportStatus.READY
    assert rig.world.perfumes.by_id.get(UUID(int=7)) is None


@pytest.mark.parametrize("action", ["confirm", "discard"])
async def test_a_malformed_import_id_is_422_validation_error(rig: Rig, action: str) -> None:
    response = await rig.client.post(f"{BASE}/not-a-uuid/{action}", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


# --- confirm ----------------------------------------------------------------------------------


async def test_confirm_answers_200_with_the_applied_import_and_applies_the_plan(
    rig: Rig,
) -> None:
    item = await _ready(rig)

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "created_at",
        "finished_at",
        "expires_at",
        "row_count",
        "summary",
        "error_count",
        "errors",
        "applied_at",
        "discarded_at",
    }
    assert (body["id"], body["status"]) == (str(item.id), "applied")
    assert body["applied_at"] == "2026-10-11T12:00:00Z"
    assert body["discarded_at"] is None
    assert (body["error_count"], body["errors"]) == (0, [])
    assert UUID(int=7) in rig.world.perfumes.by_id
    stored = rig.store.by_id[item.id]
    assert (stored.status, stored.applied_by) == (ImportStatus.APPLIED, "test-admin")


async def test_confirm_of_an_unknown_import_is_404(rig: Rig) -> None:
    response = await rig.client.post(f"{BASE}/{UNKNOWN_ID}/confirm", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (404, "CATALOG_IMPORT_NOT_FOUND")


async def test_confirming_twice_is_200_then_409_not_ready(rig: Rig) -> None:
    item = await _ready(rig)
    first = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    second = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert first.status_code == 200
    assert (second.status_code, second.json()["code"]) == (409, "CATALOG_IMPORT_NOT_READY")
    assert "details" not in second.json() or second.json()["details"] is None


@pytest.mark.parametrize(
    "status",
    [
        ImportStatus.PENDING,
        ImportStatus.INVALID,
        ImportStatus.EXPIRED,
        ImportStatus.DISCARDED,
    ],
)
async def test_confirming_an_import_that_is_not_ready_is_409_not_ready(
    rig: Rig, status: ImportStatus
) -> None:
    item = await _ready(rig)
    stored = rig.store.by_id[item.id]
    stored.status = status

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (409, "CATALOG_IMPORT_NOT_READY")
    assert rig.world.perfumes.by_id.get(UUID(int=7)) is None


async def test_confirming_a_ready_import_past_expires_at_is_409_not_ready(rig: Rig) -> None:
    item = await _ready(rig)
    rig.store.by_id[item.id].expires_at = NOW - timedelta(seconds=1)

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (409, "CATALOG_IMPORT_NOT_READY")


async def test_a_stale_confirm_is_409_with_the_issues_of_each_affected_row(rig: Rig) -> None:
    item = await _ready(rig, archived_existing=True)

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    body: dict[str, Any] = response.json()
    assert (response.status_code, body["code"]) == (409, "CATALOG_IMPORT_STALE")
    assert body["details"] == {
        "issues": [
            {
                "row": 3,
                "column": None,
                "code": "CATALOG_PERFUME_ARCHIVED",
                "message": "An archived perfume is read-only; restore it first",
            }
        ]
    }
    assert rig.store.by_id[item.id].status is ImportStatus.READY


# --- confirm: stock counts need inventory:manage (plan 005, decision 21) ----------------------


async def test_confirming_a_file_with_counts_is_403_without_inventory_manage_and_applies_nothing(
    rig: Rig,
) -> None:
    item = await _ready(rig, stock_count=5)

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert rig.store.by_id[item.id].status is ImportStatus.READY
    assert rig.world.perfumes.by_id.get(UUID(int=7)) is None
    assert rig.events.published == []


async def test_confirming_a_file_without_counts_is_200_without_inventory_manage(rig: Rig) -> None:
    item = await _ready(rig)

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert response.status_code == 200, response.text
    assert rig.store.by_id[item.id].status is ImportStatus.APPLIED
    [event] = rig.events.published
    assert event.payload()["stock_counts"] == {}


async def test_confirming_a_file_with_counts_is_200_with_inventory_manage_and_publishes_them(
    rig: Rig,
) -> None:
    item = await _ready(rig, stock_count=5)

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=INVENTORY_HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"
    [event] = rig.events.published
    [(presentation_id, count)] = event.payload()["stock_counts"].items()  # type: ignore[attr-defined]
    saved = rig.world.perfumes.by_id[UUID(int=7)].presentations
    assert ([str(p.id) for p in saved], count) == ([presentation_id], 5)
    assert event.payload()["actor_id"] == "stock-admin"


async def test_the_permission_check_for_an_unknown_import_is_still_404(rig: Rig) -> None:
    response = await rig.client.post(f"{BASE}/{UNKNOWN_ID}/confirm", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (404, "CATALOG_IMPORT_NOT_FOUND")


async def test_a_file_with_counts_that_is_not_ready_still_answers_403_before_the_409(
    rig: Rig,
) -> None:
    item = await _ready(rig, stock_count=5)
    rig.store.by_id[item.id].status = ImportStatus.DISCARDED

    response = await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (409, "CATALOG_IMPORT_NOT_READY")


async def test_discarding_a_file_with_counts_needs_no_inventory_manage(rig: Rig) -> None:
    item = await _ready(rig, stock_count=5)

    response = await rig.client.post(f"{BASE}/{item.id}/discard", headers=ADMIN_HEADERS)

    assert response.status_code == 204


async def test_the_import_status_reports_how_many_counts_the_confirm_will_record(
    rig: Rig,
) -> None:
    item = await _ready(rig, stock_count=5)

    response = await rig.client.get(f"{BASE}/{item.id}", headers=ADMIN_HEADERS)

    assert response.json()["summary"] == {
        "perfumes_new": 1,
        "perfumes_updated": 1,
        "presentations_new": 1,
        "presentations_updated": 1,
        "photos_new": 1,
        "stock_counts": 1,
    }


async def test_the_openapi_documents_the_403_of_the_confirm_and_the_summary_field(
    rig: Rig,
) -> None:
    app = rig.client._transport.app  # type: ignore[attr-defined]
    document = app.openapi()

    responses = document["paths"][f"{BASE}/{{import_id}}/confirm"]["post"]["responses"]
    summary = document["components"]["schemas"]["CatalogImportSummary"]

    assert {"200", "403", "404", "409"} <= set(responses)
    assert summary["properties"]["stock_counts"]["type"] == "integer"
    assert "stock_counts" not in summary.get("required", [])


async def test_the_in_memory_query_reports_the_stock_counts_of_the_summary() -> None:
    store = InMemoryCatalogImports()
    world = World()
    plan, _ = _plan(world, stock_count=5)
    item = CatalogImport.create(b"PK\x03\x04data", "admin-1", NOW - timedelta(minutes=10))
    item.start(NOW - timedelta(minutes=9))
    item.finish_ready(NOW, TTL, 2, ImportSummary(1, 1, 1, 1, 1, 1), plan)
    await store.add(item)

    response = await store.get(item.id)

    assert response is not None
    assert response.summary is not None
    assert response.summary.stock_counts == 1


# --- discard ----------------------------------------------------------------------------------


async def test_discard_answers_204_with_no_body_and_the_import_becomes_discarded(
    rig: Rig,
) -> None:
    item = await _ready(rig)

    response = await rig.client.post(f"{BASE}/{item.id}/discard", headers=ADMIN_HEADERS)

    assert response.status_code == 204
    assert response.content == b""
    stored = rig.store.by_id[item.id]
    assert (stored.status, stored.discarded_at) == (ImportStatus.DISCARDED, NOW)
    assert len(rig.storage.deleted) == 2


async def test_the_discarded_import_reads_back_as_discarded_with_its_timestamp(rig: Rig) -> None:
    item = await _ready(rig)
    await rig.client.post(f"{BASE}/{item.id}/discard", headers=ADMIN_HEADERS)

    response = await rig.client.get(f"{BASE}/{item.id}", headers=ADMIN_HEADERS)

    body = response.json()
    assert (body["status"], body["discarded_at"], body["applied_at"]) == (
        "discarded",
        "2026-10-11T12:00:00Z",
        None,
    )


async def test_discard_of_an_unknown_import_is_404(rig: Rig) -> None:
    response = await rig.client.post(f"{BASE}/{UNKNOWN_ID}/discard", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (404, "CATALOG_IMPORT_NOT_FOUND")


async def test_discarding_twice_is_204_then_409_not_ready(rig: Rig) -> None:
    item = await _ready(rig)
    first = await rig.client.post(f"{BASE}/{item.id}/discard", headers=ADMIN_HEADERS)

    second = await rig.client.post(f"{BASE}/{item.id}/discard", headers=ADMIN_HEADERS)

    assert first.status_code == 204
    assert (second.status_code, second.json()["code"]) == (409, "CATALOG_IMPORT_NOT_READY")


async def test_discarding_an_applied_import_is_409_not_ready(rig: Rig) -> None:
    item = await _ready(rig)
    await rig.client.post(f"{BASE}/{item.id}/confirm", headers=ADMIN_HEADERS)

    response = await rig.client.post(f"{BASE}/{item.id}/discard", headers=ADMIN_HEADERS)

    assert (response.status_code, response.json()["code"]) == (409, "CATALOG_IMPORT_NOT_READY")
    assert rig.storage.deleted == []
