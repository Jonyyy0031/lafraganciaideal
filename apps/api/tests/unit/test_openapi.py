"""The committed OpenAPI document is accurate and up to date."""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.main.openapi import OPENAPI_FILE, document, render
from fragancia_api.shared.http.reference import SCALAR_JS_URL
from fragancia_api.shared.http.services import ServiceRegistry


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return document()


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
    ]


def test_committed_document_is_up_to_date(spec: dict[str, Any]) -> None:
    assert OPENAPI_FILE.read_text() == render(spec), (
        "apps/api/openapi.json is stale: run `uv run just openapi` and commit it"
    )


def test_only_admin_operations_declare_401_and_403(spec: dict[str, Any]) -> None:
    for _, path, operation in _operations(spec):
        is_admin = path.startswith("/api/v1/admin")
        declares = {"401", "403"} <= operation["responses"].keys()
        assert declares == is_admin, path


def test_422_uses_the_api_error_shape(spec: dict[str, Any]) -> None:
    error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
    for _, path, operation in _operations(spec):
        if "422" in operation["responses"]:
            content = operation["responses"]["422"]["content"]["application/json"]
            assert content["schema"] == error_ref, path
    assert not {"HTTPValidationError", "ValidationError"} & spec["components"]["schemas"].keys()


def test_operation_ids_are_readable_and_unique(spec: dict[str, Any]) -> None:
    ids = [operation["operationId"] for *_, operation in _operations(spec)]
    assert len(ids) == len(set(ids))
    assert {"liveness", "readiness", "list_brands", "create_brand", "list_all_brands"} <= set(ids)
    assert all("api_v1" not in operation_id for operation_id in ids)


def test_schema_names_are_readable(spec: dict[str, Any]) -> None:
    names = spec["components"]["schemas"].keys()
    assert "AdminBrandPage" in names
    assert not [name for name in names if "_" in name or "[" in name]


def test_document_describes_the_api(spec: dict[str, Any]) -> None:
    assert spec["info"]["title"] == "La Fragancia Ideal API"
    assert "Bearer" in spec["info"]["description"]
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"


async def test_scalar_reference_is_served_with_a_pinned_script() -> None:
    app = build_app(ServiceRegistry(), [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert SCALAR_JS_URL in response.text and "@1." in SCALAR_JS_URL
    assert "/api/v1/openapi.json" in response.text
    assert "/api/v1/docs" not in document()["paths"]


async def test_reference_and_document_are_hidden_without_docs() -> None:
    app = build_app(ServiceRegistry(), [], docs=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/api/v1/docs")).status_code == 404
        assert (await client.get("/api/v1/openapi.json")).status_code == 404
