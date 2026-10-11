"""Plan 007: `HttpImageDownloader` against a local HTTP server (loopback), with the SSRF guard on
(default) and off (`allow_private_hosts`, which is how a local server can be reached at all)."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from fragancia_api.modules.catalog.infrastructure.http_image_downloader import (
    MAX_REDIRECTS,
    HttpImageDownloader,
)
from fragancia_api.shared.kernel import Err, Ok, Result

pytestmark = pytest.mark.integration

BODY = b"image-bytes" * 10


@dataclass
class Server:
    port: int
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


async def _handle(
    server: Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    lines = head.split("\r\n")
    path = lines[0].split(" ")[1]
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
    server.requests.append((path, headers))
    if path == "/ok":
        response = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(BODY) + BODY
    elif path == "/missing":
        response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
    elif path == "/redirect":
        response = b"HTTP/1.1 302 Found\r\nLocation: /ok\r\nContent-Length: 0\r\n\r\n"
    elif path == "/loop":
        response = b"HTTP/1.1 302 Found\r\nLocation: /loop\r\nContent-Length: 0\r\n\r\n"
    elif path == "/declared-big":
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"
    elif path == "/streamed-big":
        response = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"x" * 5000
    else:
        response = b"HTTP/1.1 500 Oops\r\nContent-Length: 0\r\n\r\n"
    writer.write(response)
    await writer.drain()
    writer.close()


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    holder = Server(port=0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(holder, reader, writer)

    started = await asyncio.start_server(handle, "127.0.0.1", 0)
    holder.port = started.sockets[0].getsockname()[1]
    yield holder
    started.close()
    await started.wait_closed()


def _open(max_bytes: int = 1000) -> HttpImageDownloader:
    return HttpImageDownloader(max_bytes=max_bytes, allow_private_hosts=True)


def _code(result: Result[bytes, object]) -> str:
    assert isinstance(result, Err), result
    return str(result.error.code)  # type: ignore[attr-defined]


async def test_a_200_returns_the_body_and_sends_the_original_host_header(server: Server) -> None:
    result = await _open().fetch(server.url("/ok"))

    assert result == Ok(BODY)
    [(path, headers)] = server.requests
    assert path == "/ok"
    assert headers["host"] == f"127.0.0.1:{server.port}"


async def test_a_404_is_unreachable_with_the_status(server: Server) -> None:
    result = await _open().fetch(server.url("/missing"))

    assert _code(result) == "CATALOG_IMPORT_IMAGE_UNREACHABLE"
    assert isinstance(result, Err)
    assert result.error.details == {"status": 404}


async def test_a_redirect_is_followed_by_hand(server: Server) -> None:
    result = await _open().fetch(server.url("/redirect"))

    assert result == Ok(BODY)
    assert [path for path, _ in server.requests] == ["/redirect", "/ok"]


async def test_an_endless_redirect_stops_after_three_hops_as_unreachable(server: Server) -> None:
    result = await _open().fetch(server.url("/loop"))

    assert _code(result) == "CATALOG_IMPORT_IMAGE_UNREACHABLE"
    assert len(server.requests) == MAX_REDIRECTS + 1 == 4


async def test_a_declared_length_over_the_limit_is_too_large(server: Server) -> None:
    assert _code(await _open(max_bytes=1000).fetch(server.url("/declared-big"))) == (
        "CATALOG_PHOTO_TOO_LARGE"
    )


async def test_a_streamed_body_over_the_limit_is_too_large(server: Server) -> None:
    assert _code(await _open(max_bytes=1000).fetch(server.url("/streamed-big"))) == (
        "CATALOG_PHOTO_TOO_LARGE"
    )


async def test_a_body_exactly_at_the_limit_is_accepted(server: Server) -> None:
    assert await _open(max_bytes=len(BODY)).fetch(server.url("/ok")) == Ok(BODY)


async def test_a_closed_port_is_unreachable() -> None:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    result = await _open().fetch(f"http://127.0.0.1:{port}/x")

    assert _code(result) == "CATALOG_IMPORT_IMAGE_UNREACHABLE"


# --- the SSRF guard (default settings) --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.jpg",
        "http://127.0.0.1:9/a.jpg",
        "http://[::1]/a.jpg",
        "http://10.0.0.5/a.jpg",
        "http://192.168.1.10/a.jpg",
        "http://172.16.0.1/a.jpg",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/a.jpg",
        "http://224.0.0.1/a.jpg",
        "http://localhost/a.jpg",
    ],
)
async def test_private_loopback_link_local_and_multicast_hosts_are_blocked_by_default(
    url: str,
) -> None:
    result = await HttpImageDownloader(max_bytes=1000, allow_private_hosts=False).fetch(url)

    assert _code(result) == "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"


async def test_the_default_guard_never_contacts_a_loopback_server(server: Server) -> None:
    result = await HttpImageDownloader(max_bytes=1000, allow_private_hosts=False).fetch(
        server.url("/ok")
    )

    assert _code(result) == "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"
    assert server.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/a.jpg",
        "file:///etc/passwd",
        "http:///a.jpg",
        "http://user:pass@example.test/a.jpg",
        "http://example.test:notaport/a.jpg",
        "not a url",
    ],
)
async def test_non_http_urls_userinfo_and_bad_ports_are_invalid(url: str) -> None:
    assert _code(await _open().fetch(url)) == "CATALOG_IMPORT_IMAGE_URL_INVALID"


@pytest.mark.skip(
    reason="NOT CONFIRMED: a redirect from a public host to a private one needs a public "
    "first hop, and the sandbox has no public server to point at; the re-check of every hop "
    "is the same `_fetch_once` path the blocked-host tests above cover"
)
async def test_a_redirect_to_a_private_host_is_blocked() -> None: ...


@pytest.mark.skip(
    reason="NOT CONFIRMED: the read timeout is 20 s and the whole fetch 30 s; a timeout test "
    "would stall the suite that long"
)
async def test_a_server_that_never_answers_times_out_as_unreachable() -> None: ...
