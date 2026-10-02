"""The OpenAPI document as clients see it.

FastAPI's defaults are adjusted so the document matches what the API really answers:
- operation ids are the route function names (`list_brands`), which become method names in
  the generated web client, so they must be unique and readable;
- 422 responses use `ErrorResponse` (our `VALIDATION_ERROR` / domain errors), not FastAPI's
  `HTTPValidationError`, which this API never returns.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from fragancia_api.shared.http.errors import ErrorResponse

_ERROR_REF = "#/components/schemas/ErrorResponse"
_FASTAPI_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")


def operation_id(route: APIRoute) -> str:
    return route.name


def install_openapi(app: FastAPI, *, description: str) -> None:
    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = default_openapi()
            schema["info"]["description"] = description
            _use_error_response_for_422(schema)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _use_error_response_for_422(schema: dict[str, Any]) -> None:
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            response = operation.get("responses", {}).get("422")
            if response is None:
                continue
            response["description"] = "Invalid input or a broken business rule"
            response["content"] = {"application/json": {"schema": {"$ref": _ERROR_REF}}}
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for name in _FASTAPI_VALIDATION_SCHEMAS:
        schemas.pop(name, None)
    schemas.setdefault("ErrorResponse", ErrorResponse.model_json_schema())
