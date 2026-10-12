import uuid

import structlog
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fragancia_api.shared.http.errors import error_response

log = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class BodyTooLarge(Exception):
    """Raised from the wrapped `receive` once a streamed body passes the limit."""


def _media_type(scope: Scope) -> str:
    value = Headers(scope=scope).get("content-type", "")
    return value.split(";", 1)[0].strip().lower()


def _is_bounded_text(media_type: str) -> bool:
    return (
        media_type == ""
        or media_type == "application/json"
        or media_type.endswith("+json")
        or media_type in ("application/x-www-form-urlencoded", "multipart/form-data")
        or media_type.startswith("text/")
    )


class BodySizeLimitMiddleware:
    """Pure ASGI middleware that refuses oversized request bodies with 413 `REQUEST_TOO_LARGE`.

    JSON, form, text and body-less requests get `max_body_bytes`; any other media type is a
    binary upload and gets `max_upload_bytes`. A declared `Content-Length` over the limit is
    refused without reading the body; a streamed (chunked) body is cut off once it passes it.
    It knows no route paths: the upload routes keep their own, smaller limits."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, max_upload_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = (
            self.max_body_bytes if _is_bounded_text(_media_type(scope)) else self.max_upload_bytes
        )
        headers = Headers(scope=scope)
        if REQUEST_ID_HEADER.lower() not in headers:
            # One id for the whole request: the inner RequestContextMiddleware reads it from
            # the scope, so the 413 header matches the id bound to the logs.
            scope = {
                **scope,
                "headers": [
                    *scope["headers"],
                    (REQUEST_ID_HEADER.lower().encode(), uuid.uuid4().hex.encode()),
                ],
            }
        declared = headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            await self._refuse(scope, receive, send, limit)
            return

        received = 0
        response_started = False
        exceeded = False
        replaced = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started, replaced
            if replaced:
                return
            if message["type"] == "http.response.start":
                if exceeded and not response_started:
                    # The app swallowed the error raised by `limited_receive` (FastAPI turns it
                    # into a 400 while parsing the body): the answer is still the 413.
                    replaced = response_started = True
                    await self._refuse(scope, receive, send, limit)
                    return
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if response_started:
                raise
            await self._refuse(scope, receive, send, limit)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        # The scope carries the request id (client-sent, or generated in `__call__`).
        request_id = Headers(scope=scope).get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        response = error_response(
            413, "REQUEST_TOO_LARGE", "The request body is too large", {"max_bytes": limit}
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        await response(scope, receive, send)


def _only_body_too_large(error: BaseException) -> bool:
    if isinstance(error, BodyTooLarge):
        return True
    return (
        isinstance(error, BaseExceptionGroup)
        and error.subgroup(BodyTooLarge) is not None
        and error.split(BodyTooLarge)[1] is None
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Gives every request an id (taken from `X-Request-ID` or generated), binds it to the
    logs, echoes it in the response, and turns unexpected exceptions into a 500 without
    details (the full error is logged)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        except Exception as error:
            if _only_body_too_large(error):
                # A cut-off body is not an error: BodySizeLimitMiddleware answers the 413.
                # `BaseHTTPMiddleware` may wrap it in an exception group; unwrap it so the
                # outer middleware sees a plain `BodyTooLarge`.
                raise BodyTooLarge from error
            log.exception("request.unhandled_error", method=request.method, path=request.url.path)
            response = error_response(500, "INTERNAL_ERROR", "Unexpected error")
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
