"""Plan 003: the request body size limit, alone (raw ASGI) and through `build_app`."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

import pytest
import structlog.testing
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from fragancia_api.container import build_container
from fragancia_api.main.http import build_app, create_app
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import admin_router, public_router, require_admin
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.middleware import BodySizeLimitMiddleware
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import ADMIN_HEADERS, TestActorResolver, make_settings

BODY_LIMIT = 100
UPLOAD_LIMIT = 1000


# --- the middleware alone ------------------------------------------------------------------


async def _drain_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Reads the whole body, then answers 200 with the byte count."""
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _middleware(app: Callable[..., Awaitable[None]] = _drain_app) -> BodySizeLimitMiddleware:
    return BodySizeLimitMiddleware(app, max_body_bytes=BODY_LIMIT, max_upload_bytes=UPLOAD_LIMIT)


def _scope(headers: dict[str, str] | None = None, scope_type: str = "http") -> Scope:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return {"type": scope_type, "method": "POST", "path": "/x", "headers": raw}


async def _run(
    middleware: BodySizeLimitMiddleware, scope: Scope, chunks: list[bytes]
) -> tuple[list[Message], int]:
    """Drive the middleware; returns the sent messages and how many chunks were consumed."""
    queue = list(chunks) or [b""]
    consumed = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal consumed
        chunk = queue.pop(0)
        consumed += 1
        return {"type": "http.request", "body": chunk, "more_body": bool(queue)}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent, consumed


def _status(sent: list[Message]) -> int:
    return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))


def _json_body(sent: list[Message]) -> Any:
    return json.loads(
        b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    )


def _header(sent: list[Message], name: str) -> str | None:
    start = next(m for m in sent if m["type"] == "http.response.start")
    for key, value in start["headers"]:
        if key.decode().lower() == name.lower():
            return str(value.decode())
    return None


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "Application/JSON; charset=utf-8",
        "application/problem+json",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=abc",
        "text/plain",
        None,
    ],
)
async def test_text_like_media_types_get_the_body_limit(content_type: str | None) -> None:
    headers = {"content-type": content_type} if content_type else {}
    sent, _ = await _run(_middleware(), _scope(headers), [b"x" * (BODY_LIMIT + 1)])
    assert _status(sent) == 413
    assert _json_body(sent)["details"] == {"max_bytes": BODY_LIMIT}


@pytest.mark.parametrize("content_type", ["image/jpeg", "application/octet-stream"])
async def test_binary_media_types_get_the_larger_upload_limit(content_type: str) -> None:
    middleware = _middleware()
    headers = {"content-type": content_type}
    within, _ = await _run(middleware, _scope(headers), [b"x" * (BODY_LIMIT + 1)])
    assert _status(within) == 200
    over, _ = await _run(middleware, _scope(headers), [b"x" * (UPLOAD_LIMIT + 1)])
    assert _status(over) == 413
    assert _json_body(over)["details"] == {"max_bytes": UPLOAD_LIMIT}


async def test_the_xlsx_media_type_is_binary() -> None:
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    sent, _ = await _run(_middleware(), _scope({"content-type": xlsx}), [b"x" * (BODY_LIMIT + 1)])
    assert _status(sent) == 200


async def test_a_body_exactly_at_the_limit_passes() -> None:
    sent, _ = await _run(_middleware(), _scope(), [b"x" * BODY_LIMIT])
    assert _status(sent) == 200
    assert _json_body(sent) == BODY_LIMIT


async def test_a_declared_length_at_the_limit_passes() -> None:
    headers = {"content-length": str(BODY_LIMIT)}
    sent, _ = await _run(_middleware(), _scope(headers), [b"x" * BODY_LIMIT])
    assert _status(sent) == 200


async def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body() -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    headers = {"content-length": str(BODY_LIMIT + 1)}
    sent, consumed = await _run(_middleware(app), _scope(headers), [b"x"])
    assert (_status(sent), consumed, called) == (413, 0, False)
    assert _json_body(sent) == {
        "code": "REQUEST_TOO_LARGE",
        "message": "The request body is too large",
        "details": {"max_bytes": BODY_LIMIT},
    }


@pytest.mark.parametrize("declared", ["abc", "-5", "", "１２３４５６"])
async def test_a_malformed_content_length_falls_back_to_counting_the_stream(
    declared: str,
) -> None:
    sent, _ = await _run(_middleware(), _scope({"content-length": declared}), [b"x" * 10])
    assert _status(sent) == 200


async def test_a_chunked_body_is_cut_off_once_it_passes_the_limit() -> None:
    chunks = [b"x" * 60, b"x" * 60, b"x" * 60, b"x" * 60]
    sent, consumed = await _run(_middleware(), _scope(), chunks)
    assert _status(sent) == 413
    assert consumed == 2  # the third and fourth chunks are never read


async def test_a_chunked_body_under_the_limit_passes_in_full() -> None:
    sent, _ = await _run(_middleware(), _scope(), [b"x" * 40, b"x" * 40])
    assert (_status(sent), _json_body(sent)) == (200, 80)


async def test_the_refusal_carries_a_request_id_echoing_the_requests_own() -> None:
    headers = {"content-length": "999", "x-request-id": "abc-123"}
    sent, _ = await _run(_middleware(), _scope(headers), [b""])
    assert _header(sent, "X-Request-ID") == "abc-123"


async def test_the_refusal_generates_a_request_id_when_the_request_has_none() -> None:
    sent, _ = await _run(_middleware(), _scope({"content-length": "999"}), [b""])
    request_id = _header(sent, "X-Request-ID")
    assert request_id is not None and len(request_id) == 32


async def test_exceeding_the_limit_replaces_an_error_the_app_answers_itself() -> None:
    """FastAPI answers 400 when `receive` raises while it parses the body (deviation 2)."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
            await receive()
        except Exception:  # noqa: BLE001 - mimics the framework swallowing the error
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"parse error"})

    sent, _ = await _run(_middleware(app), _scope(), [b"x" * 80, b"x" * 80])
    assert _status(sent) == 413
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert _json_body(sent)["code"] == "REQUEST_TOO_LARGE"


async def test_the_app_messages_after_the_replacement_are_dropped() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception:  # noqa: BLE001
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"a", "more_body": True})
            await send({"type": "http.response.body", "body": b"b"})

    sent, _ = await _run(_middleware(app), _scope(), [b"x" * 200])
    assert _status(sent) == 413
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert _json_body(sent)["code"] == "REQUEST_TOO_LARGE"  # parses: no stray "ab" appended


async def test_a_response_already_started_when_the_limit_is_passed_reraises() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await receive()

    with pytest.raises(Exception, match=r"^$") as raised:
        await _run(_middleware(app), _scope(), [b"x" * 80, b"x" * 80])
    assert type(raised.value).__name__ == "BodyTooLarge"


async def test_an_app_that_never_reads_the_body_is_not_refused_by_the_stream_count() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent, consumed = await _run(_middleware(app), _scope(), [b"x" * 500])
    assert (_status(sent), consumed) == (204, 0)


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_non_http_scopes_pass_through_untouched(scope_type: str) -> None:
    seen: list[Scope] = []
    received: list[Receive] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        received.append(receive)

    async def receive() -> Message:
        return {"type": "x"}

    async def send(message: Message) -> None:
        raise AssertionError("nothing should be sent")

    scope = _scope({"content-length": "999999"}, scope_type)
    await _middleware(app)(scope, receive, send)
    assert seen == [scope]
    assert received == [receive]  # the very same receive: not wrapped


# --- through build_app ---------------------------------------------------------------------


SEEN_REQUEST_IDS: list[str] = []


class Payload(BaseModel):
    name: str


def _routers() -> list[APIRouter]:
    public = public_router(prefix="/probe")

    @public.post("/echo")
    async def echo(body: Payload) -> Payload:
        return body

    @public.post("/stream")
    async def stream(request: Request) -> dict[str, int]:
        SEEN_REQUEST_IDS.append(request.headers.get("x-request-id", ""))
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"size": total}

    @public.post("/upload")
    async def upload(raw: bytes) -> dict[str, int]:  # pragma: no cover - never reached
        return {"size": len(raw)}

    admin = admin_router(prefix="/probe")

    @admin.post("/echo")
    async def admin_echo(body: Payload, actor: Annotated[Actor, Depends(require_admin)]) -> Payload:
        return body

    return [public, admin]


def _app(*, resolver: ActorResolver | None = None) -> FastAPI:
    async def ok() -> None:
        return None

    services = ServiceRegistry()
    services.add(ActorResolver, resolver or TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks(database=ok, valkey=ok))
    return build_app(services, _routers(), max_body_bytes=BODY_LIMIT, max_upload_bytes=UPLOAD_LIMIT)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        yield c


def _big_json() -> bytes:
    return json.dumps({"name": "x" * (BODY_LIMIT * 2)}).encode()


async def test_an_oversized_json_body_gets_413_with_the_error_shape(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/probe/echo", content=_big_json(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json() == {
        "code": "REQUEST_TOO_LARGE",
        "message": "The request body is too large",
        "details": {"max_bytes": BODY_LIMIT},
    }
    assert response.headers["X-Request-ID"]


async def test_the_413_echoes_the_request_id(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/probe/echo",
        content=_big_json(),
        headers={"Content-Type": "application/json", "X-Request-ID": "rid-7"},
    )
    assert (response.status_code, response.headers["X-Request-ID"]) == (413, "rid-7")


async def test_a_chunked_json_body_gets_413_not_the_frameworks_400(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        body = _big_json()
        for i in range(0, len(body), 50):
            yield body[i : i + 50]

    response = await client.post(
        "/api/v1/probe/echo", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "REQUEST_TOO_LARGE"
    assert response.headers["X-Request-ID"]


async def test_an_oversized_body_to_an_admin_route_without_a_session_is_413_not_401(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/probe/echo", content=_big_json(), headers={"Content-Type": "application/json"}
    )
    anonymous = await client.post(
        "/api/v1/admin/probe/echo",
        content=_big_json(),
        headers={"Content-Type": "application/json"},
    )
    assert (response.status_code, anonymous.status_code) == (413, 413)


async def test_a_normal_body_behaves_as_before(client: AsyncClient) -> None:
    ok = await client.post("/api/v1/probe/echo", json={"name": "ana"})
    assert (ok.status_code, ok.json()) == (200, {"name": "ana"})
    anonymous = await client.post("/api/v1/admin/probe/echo", json={"name": "ana"})
    assert anonymous.status_code == 401
    admin = await client.post(
        "/api/v1/admin/probe/echo", json={"name": "ana"}, headers=ADMIN_HEADERS
    )
    assert admin.status_code == 200


async def test_a_binary_body_over_the_json_limit_but_under_the_upload_limit_reaches_the_app(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/probe/upload",
        content=b"x" * (BODY_LIMIT + 50),
        headers={"Content-Type": "image/jpeg"},
    )
    assert response.status_code != 413


async def test_a_binary_body_over_the_upload_limit_gets_413(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/probe/upload",
        content=b"x" * (UPLOAD_LIMIT + 1),
        headers={"Content-Type": "image/jpeg"},
    )
    assert response.status_code == 413
    assert response.json()["details"] == {"max_bytes": UPLOAD_LIMIT}


async def test_get_requests_without_a_body_are_unaffected(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200


async def test_build_app_defaults_are_1_mib_and_10_mib() -> None:
    async def ok() -> None:
        return None

    services = ServiceRegistry()
    services.add(ActorResolver, TestActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks(database=ok, valkey=ok))
    app = build_app(services, _routers())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        json_over = await c.post(
            "/api/v1/probe/echo",
            content=b"x" * (1_048_576 + 1),
            headers={"Content-Type": "application/json"},
        )
        binary_over = await c.post(
            "/api/v1/probe/upload",
            content=b"x" * (10_485_760 + 1),
            headers={"Content-Type": "image/jpeg"},
        )
    assert json_over.json()["details"] == {"max_bytes": 1_048_576}
    assert binary_over.json()["details"] == {"max_bytes": 10_485_760}


async def test_create_app_wires_the_limits_from_settings() -> None:
    settings = make_settings(
        request_max_bytes=64, photo_max_bytes=500, catalog_import_max_bytes=300
    )
    app = create_app(build_container(settings))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        json_over = await c.post(
            "/api/v1/auth/login",
            content=b"x" * 65,
            headers={"Content-Type": "application/json"},
        )
        binary_over = await c.post(
            "/api/v1/auth/login",
            content=b"x" * 501,
            headers={"Content-Type": "image/jpeg"},
        )
    assert json_over.status_code == 413
    assert json_over.json()["details"] == {"max_bytes": 64}
    assert binary_over.status_code == 413
    assert binary_over.json()["details"] == {"max_bytes": 500}


def test_settings_defaults_and_derived_upload_limit() -> None:
    settings = make_settings()
    assert settings.request_max_bytes == 1_048_576
    assert settings.upload_max_bytes == 10_485_760
    assert make_settings(photo_max_bytes=100, catalog_import_max_bytes=200).upload_max_bytes == 200


def test_settings_reject_a_non_positive_request_limit() -> None:
    with pytest.raises(ValueError, match="request_max_bytes"):
        make_settings(request_max_bytes=0)


# --- repair round 1: raw-body routes and the request id --------------------------------------


async def _chunked_oversized(client: AsyncClient, headers: dict[str, str]) -> Any:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(UPLOAD_LIMIT // 50 + 2):
            yield b"x" * 50

    SEEN_REQUEST_IDS.clear()
    return await client.post(
        "/api/v1/probe/stream",
        content=chunks(),
        headers={"Content-Type": "image/jpeg", **headers},
    )


async def test_a_chunked_oversized_body_to_a_raw_route_logs_no_unhandled_error(
    client: AsyncClient,
) -> None:
    with structlog.testing.capture_logs() as logs:
        response = await _chunked_oversized(client, {})
    assert response.status_code == 413
    assert response.json()["code"] == "REQUEST_TOO_LARGE"
    assert [entry for entry in logs if entry["event"] == "request.unhandled_error"] == []


async def test_the_413_request_id_is_the_one_the_app_saw_when_the_client_sent_none(
    client: AsyncClient,
) -> None:
    response = await _chunked_oversized(client, {})
    assert response.status_code == 413
    assert len(SEEN_REQUEST_IDS) == 1
    assert response.headers["X-Request-ID"] == SEEN_REQUEST_IDS[0]
    assert len(SEEN_REQUEST_IDS[0]) == 32


async def test_a_client_sent_request_id_is_kept_through_the_cut_off(client: AsyncClient) -> None:
    response = await _chunked_oversized(client, {"X-Request-ID": "client-id-9"})
    assert response.status_code == 413
    assert SEEN_REQUEST_IDS == ["client-id-9"]
    assert response.headers["X-Request-ID"] == "client-id-9"
