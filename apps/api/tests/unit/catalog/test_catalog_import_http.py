"""Plan 007: the three import routes (template, raw-body upload, status), their access and the
size and media-type checks."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ImportUploadLimit,
    StartCatalogImport,
)
from fragancia_api.modules.catalog.application.queries.catalog_imports import (
    DownloadCatalogTemplate,
    GetCatalogImport,
)
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportIssue,
    ImportStatus,
    ImportSummary,
)
from fragancia_api.modules.catalog.domain.errors import ImportRequired
from fragancia_api.modules.catalog.http.import_router import XLSX_MEDIA_TYPE, import_routers
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryConcentrations,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_imports import InMemoryCatalogImports
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected
from tests.unit.catalog.import_support import PLAN
from tests.unit.catalog.perfume_support import make_concentration, make_family

BASE = "/api/v1/admin/catalog-imports"
MAX_BYTES = 100
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
UNKNOWN_ID = UUID(int=404)
NO_PERMISSION_SESSION = "admin-without-catalog-manage"
NO_PERMISSION_HEADERS = {"Cookie": f"{SESSION_COOKIE}={NO_PERMISSION_SESSION}"}
XLSX_HEADERS = {**ADMIN_HEADERS, "content-type": XLSX_MEDIA_TYPE}
ZIP = b"PK\x03\x04" + b"x" * 20


class _Resolver:
    async def resolve(self, token: str) -> Actor | None:
        if token == NO_PERMISSION_SESSION:
            return Actor(id="no-perm", is_admin=True, session_id=UUID(int=2))
        return await TestActorResolver().resolve(token)


class _Writer:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []

    async def build(self, families: list[str], concentrations: list[str]) -> bytes:
        self.calls.append((families, concentrations))
        return b"PK\x03\x04 the template"


@dataclass
class Rig:
    client: AsyncClient
    store: InMemoryCatalogImports
    writer: _Writer


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    store = InMemoryCatalogImports()
    writer = _Writer()
    families = InMemoryOlfactoryFamilies(
        make_family("Floral"), make_family("Cítrica", active=False)
    )
    concentrations = InMemoryConcentrations(make_concentration("Eau de Toilette", "EDT"))
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        StartCatalogImport,
        StartCatalogImport(
            imports=store, transactions=InMemoryTransactionRunner(), clock=FixedClock(NOW)
        ),
    )
    services.add(ImportUploadLimit, ImportUploadLimit(max_bytes=MAX_BYTES))
    services.add(GetCatalogImport, GetCatalogImport(store))
    services.add(
        DownloadCatalogTemplate,
        DownloadCatalogTemplate(families, concentrations, writer),
    )
    app = build_app(services, import_routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield Rig(client, store, writer)


def _code(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _upload(
    rig: Rig, body: bytes = ZIP, *, content_type: str | None = XLSX_MEDIA_TYPE
) -> Response:
    headers = dict(ADMIN_HEADERS)
    if content_type is not None:
        headers["content-type"] = content_type
    return await rig.client.post(BASE, content=body, headers=headers)


# --- access -----------------------------------------------------------------------------------

_SOME = str(UNKNOWN_ID)
_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", f"{BASE}/template", {}),
    ("POST", BASE, {"content": ZIP, "headers": {"content-type": XLSX_MEDIA_TYPE}}),
    ("GET", f"{BASE}/{_SOME}", {}),
]


@pytest.mark.parametrize(("method", "url", "kwargs"), _ROUTES)
async def test_the_import_routes_return_401_without_a_session(
    rig: Rig, method: str, url: str, kwargs: dict[str, Any]
) -> None:
    response = await rig.client.request(method, url, **kwargs)

    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")
    assert rig.store.by_id == {}
    assert rig.writer.calls == []


@pytest.mark.parametrize(("method", "url", "kwargs"), _ROUTES)
async def test_the_import_routes_return_403_for_an_admin_without_catalog_manage(
    rig: Rig, method: str, url: str, kwargs: dict[str, Any]
) -> None:
    headers = {**kwargs.get("headers", {}), **NO_PERMISSION_HEADERS}
    response = await rig.client.request(method, url, **{**kwargs, "headers": headers})

    assert _code(response) == (403, "FORBIDDEN")
    assert rig.store.by_id == {}
    assert rig.writer.calls == []


# --- template ---------------------------------------------------------------------------------


async def test_the_template_is_an_xlsx_attachment_built_from_the_active_names(rig: Rig) -> None:
    response = await rig.client.get(f"{BASE}/template", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MEDIA_TYPE
    assert (
        response.headers["content-disposition"] == 'attachment; filename="plantilla-catalogo.xlsx"'
    )
    assert response.content == b"PK\x03\x04 the template"
    assert rig.writer.calls == [(["Floral"], ["Eau de Toilette"])]


async def test_template_is_a_route_of_its_own_not_an_import_id(rig: Rig) -> None:
    response = await rig.client.get(f"{BASE}/template", headers=ADMIN_HEADERS)

    assert response.status_code == 200  # not a 422 for a non-UUID {import_id}


# --- upload -----------------------------------------------------------------------------------


async def test_an_upload_is_202_with_the_id_and_stores_a_pending_import(rig: Rig) -> None:
    response = await _upload(rig)

    assert response.status_code == 202
    assert set(response.json()) == {"id"}
    [stored] = rig.store.by_id.values()
    assert str(stored.id) == response.json()["id"]
    assert stored.status is ImportStatus.PENDING
    assert stored.file == ZIP
    assert stored.created_by == "test-admin"  # request.state.actor.id
    assert stored.created_at == NOW


async def test_the_media_type_may_carry_parameters_and_any_case(rig: Rig) -> None:
    response = await _upload(rig, content_type=XLSX_MEDIA_TYPE.upper() + "; charset=binary")

    assert response.status_code == 202


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/octet-stream",
        "text/plain",
        "text/csv",
        "application/vnd.ms-excel",
        "image/jpeg",
        None,
    ],
)
async def test_any_other_media_type_is_422_format_invalid_and_stores_nothing(
    rig: Rig, content_type: str | None
) -> None:
    response = await _upload(rig, content_type=content_type)

    assert _code(response) == (422, "CATALOG_IMPORT_FORMAT_INVALID")
    assert rig.store.by_id == {}


@pytest.mark.parametrize(
    "body", [b"not a zip file at all", b"%PDF-1.7 ...", b"PK", b"PK\x05\x06xx"]
)
async def test_a_body_that_is_not_a_zip_is_422_format_invalid(rig: Rig, body: bytes) -> None:
    response = await _upload(rig, body)

    assert _code(response) == (422, "CATALOG_IMPORT_FORMAT_INVALID")
    assert rig.store.by_id == {}


async def test_an_empty_body_is_422_format_invalid(rig: Rig) -> None:
    response = await _upload(rig, b"")

    assert _code(response) == (422, "CATALOG_IMPORT_FORMAT_INVALID")
    assert rig.store.by_id == {}


async def test_a_json_body_is_422_format_invalid_by_its_content_type(rig: Rig) -> None:
    response = await rig.client.post(BASE, json={"file": "x"}, headers=ADMIN_HEADERS)

    assert _code(response) == (422, "CATALOG_IMPORT_FORMAT_INVALID")


async def test_a_body_exactly_at_the_limit_is_accepted(rig: Rig) -> None:
    response = await _upload(rig, b"PK\x03\x04" + b"x" * (MAX_BYTES - 4))

    assert response.status_code == 202


async def test_a_body_over_the_limit_is_422_too_large_with_the_max_bytes(rig: Rig) -> None:
    response = await _upload(rig, b"PK\x03\x04" + b"x" * (MAX_BYTES - 3))

    assert _code(response) == (422, "CATALOG_IMPORT_TOO_LARGE")
    assert response.json()["details"] == {"max_bytes": MAX_BYTES}
    assert rig.store.by_id == {}


class _Stream:
    """A chunked request body that counts how many chunks the server pulled from it."""

    def __init__(self, chunks: int, size: int = 1024, first: bytes = b"") -> None:
        self.total = chunks
        self._size = size
        self._first = first
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index in range(self.total):
            self.pulled += 1
            chunk = b"x" * self._size
            yield self._first + chunk[len(self._first) :] if index == 0 else chunk


async def _stream_upload(
    rig: Rig, stream: _Stream, headers: dict[str, str] | None = None
) -> Response:
    return await rig.client.post(
        BASE,
        content=stream,
        headers={**(XLSX_HEADERS if headers is None else headers)},
    )


async def test_a_streamed_body_over_the_limit_is_422_too_large_and_cut_off(rig: Rig) -> None:
    stream = _Stream(chunks=1000)  # chunked: nobody announced its size

    response = await _stream_upload(rig, stream)

    assert _code(response) == (422, "CATALOG_IMPORT_TOO_LARGE")
    assert response.json()["details"] == {"max_bytes": MAX_BYTES}
    assert 1 <= stream.pulled < stream.total
    assert rig.store.by_id == {}


async def test_a_streamed_body_exactly_at_the_limit_is_accepted(rig: Rig) -> None:
    stream = _Stream(chunks=4, size=MAX_BYTES // 4, first=b"PK\x03\x04")

    response = await _stream_upload(rig, stream)

    assert response.status_code == 202, response.text
    [stored] = rig.store.by_id.values()
    assert stored.file is not None
    assert len(stored.file) == MAX_BYTES


async def test_a_declared_content_length_over_the_limit_is_422_without_reading_the_body(
    rig: Rig,
) -> None:
    stream = _Stream(chunks=3)

    response = await _stream_upload(
        rig, stream, {**XLSX_HEADERS, "content-length": str(MAX_BYTES + 1)}
    )

    assert _code(response) == (422, "CATALOG_IMPORT_TOO_LARGE")
    assert stream.pulled == 0


async def test_an_unauthenticated_streamed_body_is_401_and_no_chunk_is_consumed(rig: Rig) -> None:
    stream = _Stream(chunks=1000)

    response = await _stream_upload(rig, stream, headers={"content-type": XLSX_MEDIA_TYPE})

    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")
    assert stream.pulled == 0


async def test_a_streamed_body_without_catalog_manage_is_403_and_no_chunk_is_consumed(
    rig: Rig,
) -> None:
    stream = _Stream(chunks=1000)

    response = await _stream_upload(
        rig, stream, headers={**NO_PERMISSION_HEADERS, "content-type": XLSX_MEDIA_TYPE}
    )

    assert _code(response) == (403, "FORBIDDEN")
    assert stream.pulled == 0


# --- status -----------------------------------------------------------------------------------


async def test_a_pending_import_is_reported_with_empty_report_fields(rig: Rig) -> None:
    import_id = (await _upload(rig)).json()["id"]

    response = await rig.client.get(f"{BASE}/{import_id}", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "id": import_id,
        "status": "pending",
        "created_at": "2026-10-11T12:00:00Z",
        "finished_at": None,
        "expires_at": None,
        "row_count": None,
        "summary": None,
        "error_count": 0,
        "errors": [],
    }


async def test_a_ready_import_shows_the_summary_and_no_file_or_plan(rig: Rig) -> None:
    catalog_import = CatalogImport.create(ZIP, "admin", NOW)
    catalog_import.start(NOW)
    catalog_import.finish_ready(
        NOW + timedelta(minutes=1), timedelta(minutes=60), 7, ImportSummary(1, 2, 3, 4, 5), PLAN
    )
    await rig.store.add(catalog_import)

    response = await rig.client.get(f"{BASE}/{catalog_import.id}", headers=ADMIN_HEADERS)

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["summary"] == {
        "perfumes_new": 1,
        "perfumes_updated": 2,
        "presentations_new": 3,
        "presentations_updated": 4,
        "photos_new": 5,
    }
    assert (body["row_count"], body["error_count"], body["errors"]) == (7, 0, [])
    assert body["finished_at"] == "2026-10-11T12:01:00Z"
    assert body["expires_at"] == "2026-10-11T13:01:00Z"
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
    }


async def test_an_invalid_import_lists_each_issue_with_row_header_text_and_code(rig: Rig) -> None:
    catalog_import = CatalogImport.create(ZIP, "admin", NOW)
    catalog_import.start(NOW)
    catalog_import.finish_invalid(
        NOW,
        3,
        [
            ImportIssue.of(ImportRequired(), row=2, column="Marca"),
            ImportIssue.of(ImportRequired(), row=None, column=None),
        ],
    )
    await rig.store.add(catalog_import)

    body = (await rig.client.get(f"{BASE}/{catalog_import.id}", headers=ADMIN_HEADERS)).json()

    assert body["status"] == "invalid"
    assert body["error_count"] == 2
    assert body["errors"] == [
        {
            "row": 2,
            "column": "Marca",
            "code": "CATALOG_IMPORT_REQUIRED",
            "message": "This cell is required",
        },
        {
            "row": None,
            "column": None,
            "code": "CATALOG_IMPORT_REQUIRED",
            "message": "This cell is required",
        },
    ]
    assert body["summary"] is None


async def test_an_unknown_import_is_404(rig: Rig) -> None:
    response = await rig.client.get(f"{BASE}/{UNKNOWN_ID}", headers=ADMIN_HEADERS)

    assert _code(response) == (404, "CATALOG_IMPORT_NOT_FOUND")


async def test_a_malformed_import_id_is_a_validation_error(rig: Rig) -> None:
    response = await rig.client.get(f"{BASE}/not-a-uuid", headers=ADMIN_HEADERS)

    assert _code(response) == (422, "VALIDATION_ERROR")


# --- the OpenAPI contract ---------------------------------------------------------------------


async def test_the_openapi_documents_the_raw_xlsx_body_and_the_statuses(rig: Rig) -> None:
    services = ServiceRegistry()
    app = build_app(services, import_routers)
    paths = app.openapi()["paths"]

    upload = paths[BASE]["post"]
    assert upload["requestBody"]["required"] is True
    assert upload["requestBody"]["content"] == {
        XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}
    }
    assert "202" in upload["responses"]
    assert "422" in upload["responses"]
    assert set(paths[f"{BASE}/template"]["get"]["responses"]["200"]["content"]) == {XLSX_MEDIA_TYPE}
    assert "404" in paths[f"{BASE}/{{import_id}}"]["get"]["responses"]
    assert all(
        operation["tags"] == ["catalog · import"]
        for path in (BASE, f"{BASE}/template", f"{BASE}/{{import_id}}")
        for operation in paths[path].values()
    )
