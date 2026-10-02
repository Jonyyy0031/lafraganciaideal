import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from fragancia_api.shared.http.errors import error_response

log = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Gives every request an id (taken from `X-Request-ID` or generated), binds it to the
    logs, echoes it in the response, and turns unexpected exceptions into a 500 without
    details (the full error is logged)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.unhandled_error", method=request.method, path=request.url.path)
            response = error_response(500, "INTERNAL_ERROR", "Unexpected error")
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
