"""One place that turns errors into HTTP responses: always `{code, message, details?}`.

- `Err(DomainError)` from a use case → `unwrap()` → status by category (404/409/422).
- Invalid input (Pydantic) → 422 `VALIDATION_ERROR` with per-field issues.
- Missing/unknown credentials → 401 `AUTHENTICATION_REQUIRED`; not allowed → 403 `FORBIDDEN`.
- Unknown route / method → 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED`.
- Anything else is unexpected: handled by `RequestContextMiddleware` (500 `INTERNAL_ERROR`).
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from fragancia_api.shared.kernel import (
    BusinessRuleViolationError,
    ConflictError,
    DomainError,
    Err,
    InvalidValueError,
    NotFoundError,
    Ok,
    Result,
)


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class DomainErrorRaised(Exception):
    def __init__(self, error: DomainError) -> None:
        super().__init__(error.code)
        self.error = error


class AuthenticationRequired(Exception):
    pass


class Forbidden(Exception):
    pass


_STATUS_BY_CATEGORY: tuple[tuple[type[DomainError], int], ...] = (
    (NotFoundError, 404),
    (ConflictError, 409),
    (InvalidValueError, 422),
    (BusinessRuleViolationError, 422),
)

_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def status_for(error: DomainError) -> int:
    for category, status in _STATUS_BY_CATEGORY:
        if isinstance(error, category):
            return status
    return 422


def unwrap[T](result: Result[T, DomainError]) -> T:
    """Return the value of `Ok`, or raise so the error handler answers with the right status."""
    match result:
        case Ok(value):
            return value
        case Err(error):
            raise DomainErrorRaised(error)


def error_response(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body = ErrorResponse(code=code, message=message, details=details)
    return JSONResponse(status_code=status, content=body.model_dump(exclude_none=True))


def install_error_handlers(app: FastAPI) -> None:
    async def domain_error(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, DomainErrorRaised):
            raise exc
        error = exc.error
        details = dict(error.details) if error.details else None
        return error_response(status_for(error), error.code, error.message, details)

    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):
            raise exc
        issues = [
            {"location": list(issue["loc"]), "message": issue["msg"], "type": issue["type"]}
            for issue in exc.errors()
        ]
        return error_response(422, "VALIDATION_ERROR", "Invalid request", {"issues": issues})

    async def authentication_required(_: Request, __: Exception) -> JSONResponse:
        response = error_response(401, "AUTHENTICATION_REQUIRED", "Authentication required")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    async def forbidden(_: Request, __: Exception) -> JSONResponse:
        return error_response(403, "FORBIDDEN", "You are not allowed to perform this action")

    async def http_error(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, StarletteHTTPException):
            raise exc
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    app.add_exception_handler(DomainErrorRaised, domain_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(AuthenticationRequired, authentication_required)
    app.add_exception_handler(Forbidden, forbidden)
    app.add_exception_handler(StarletteHTTPException, http_error)
