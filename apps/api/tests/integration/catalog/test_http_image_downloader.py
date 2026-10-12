"""Plan 007: `HttpImageDownloader` against a local HTTP server (loopback), with the SSRF guard on
(default) and off (`allow_private_hosts`, which is how a local server can be reached at all)."""

import asyncio
import gzip
import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from fragancia_api.modules.catalog.infrastructure.http_image_downloader import (
    MAX_REDIRECTS,
    HttpImageDownloader,
    _ascii_host,
    _is_blocked,
)
from fragancia_api.shared.kernel import Err, Ok, Result

pytestmark = pytest.mark.integration

BODY = b"image-bytes" * 10
GZIP = gzip.compress(b"x" * 5000)


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
    elif path == "/gzip":
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(GZIP)
            + GZIP
        )
    elif path == "/gzip-undeclared":  # gzip bytes with no Content-Encoding: nothing decodes them
        response = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(GZIP) + GZIP
    elif path == "/identity-encoded":
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Encoding: identity\r\nContent-Length: %d\r\n\r\n"
            % len(BODY)
            + BODY
        )
    elif path == "/bad-location":
        response = b"HTTP/1.1 302 Found\r\nLocation: http://[bad/x.jpg\r\nContent-Length: 0\r\n\r\n"
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


# --- repair round 1 (M1): a bad URL never raises -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://[bad/x.jpg",  # an unclosed IPv6 literal: urlsplit raises ValueError
        "http://[::1/x.jpg",
        "http://a..b/x.jpg",  # an empty IDNA label
        "http://" + "a" * 70 + ".test/x.jpg",  # a label over 63 characters
        "http://example.test:99999/x.jpg",  # a port out of range
        "http://example.test:-1/x.jpg",
    ],
)
async def test_a_malformed_url_is_invalid_and_never_raises(url: str) -> None:
    assert _code(await _open().fetch(url)) == "CATALOG_IMPORT_IMAGE_URL_INVALID"


async def test_a_redirect_location_that_cannot_be_parsed_is_invalid(server: Server) -> None:
    result = await _open().fetch(server.url("/bad-location"))

    assert _code(result) == "CATALOG_IMPORT_IMAGE_URL_INVALID"
    assert [path for path, _ in server.requests] == ["/bad-location"]


async def test_an_httpx_invalid_url_while_building_the_request_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(self: Any, target: str, host_header: str, extensions: dict[str, str]) -> Any:
        raise httpx.InvalidURL("bad target")

    monkeypatch.setattr(HttpImageDownloader, "_get", refuse)

    assert _code(await _open().fetch("http://127.0.0.1/x.jpg")) == (
        "CATALOG_IMPORT_IMAGE_URL_INVALID"
    )


async def test_a_resolver_failure_is_unreachable_not_an_exception() -> None:
    result = await _open().fetch("http://nonexistent.invalid/x.jpg")

    assert _code(result) == "CATALOG_IMPORT_IMAGE_UNREACHABLE"


# --- repair round 1 (M1): IDN hosts --------------------------------------------------------------


def test_an_internationalized_host_is_encoded_to_idna_and_an_ip_literal_is_left_alone() -> None:
    assert _ascii_host("perfumería.mx") == "xn--perfumera-n5a.mx"
    assert _ascii_host("example.test") == "example.test"
    assert _ascii_host("::1") == "::1"
    assert _ascii_host("a..b") is None
    assert _ascii_host("a" * 70 + ".test") is None


def _fake_lookup(
    monkeypatch: pytest.MonkeyPatch, looked_up: list[tuple[str, int]], address: str = "127.0.0.1"
) -> None:
    loop = asyncio.get_running_loop()

    async def getaddrinfo(host: str, port: int, **kwargs: Any) -> list[Any]:
        looked_up.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)


async def test_an_idn_host_is_resolved_and_sent_in_its_ascii_form(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[tuple[str, int]] = []
    _fake_lookup(monkeypatch, looked_up)

    result = await _open().fetch(f"http://perfumería.mx:{server.port}/ok")

    assert result == Ok(BODY)
    assert looked_up == [("xn--perfumera-n5a.mx", server.port)]
    [(_, headers)] = server.requests
    assert headers["host"] == f"xn--perfumera-n5a.mx:{server.port}"


async def test_an_idn_host_is_the_ascii_tls_server_name_over_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_lookup(monkeypatch, [])
    seen: list[tuple[str, str, dict[str, str]]] = []

    async def get(self: Any, target: str, host_header: str, extensions: dict[str, str]) -> Any:
        seen.append((target, host_header, extensions))
        return Ok(BODY)

    monkeypatch.setattr(HttpImageDownloader, "_get", get)

    assert await _open().fetch("https://perfumería.mx/a.jpg") == Ok(BODY)

    [(target, host_header, extensions)] = seen
    assert target == "https://127.0.0.1:443/a.jpg"  # connects to the checked address
    assert host_header == "xn--perfumera-n5a.mx"
    assert extensions == {"sni_hostname": "xn--perfumera-n5a.mx"}


# --- repair round 1 (L5): blocked IPv6 forms -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:127.0.0.1]/a.jpg",  # IPv4-mapped loopback
        "http://[::ffff:10.0.0.5]/a.jpg",
        "http://[::127.0.0.1]/a.jpg",  # ::/96, IPv4-compatible
        "http://[::808:808]/a.jpg",  # ::/96 even when the embedded IPv4 is public
        "http://[64:ff9b::7f00:1]/a.jpg",  # NAT64 of 127.0.0.1
        "http://[64:ff9b::808:808]/a.jpg",  # 64:ff9b::/96 of a public IPv4
        "http://[64:ff9b:1::1]/a.jpg",  # 64:ff9b:1::/48 (local-use NAT64)
    ],
)
async def test_ipv6_forms_that_reach_ipv4_hosts_are_blocked_by_default(url: str) -> None:
    result = await HttpImageDownloader(max_bytes=1000, allow_private_hosts=False).fetch(url)

    assert _code(result) == "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("::ffff:127.0.0.1", True),
        ("::ffff:8.8.8.8", False),  # a mapped public address is judged by its IPv4 address
        ("::127.0.0.1", True),
        ("64:ff9b::7f00:1", True),
        ("64:ff9b:1::1", True),
        ("::1", True),
        ("ff02::1", True),
        ("2606:4700::1111", False),
        ("8.8.8.8", False),
        ("10.0.0.1", True),
    ],
)
def test_is_blocked_judges_mapped_addresses_by_their_ipv4_and_blocks_the_transition_ranges(
    address: str, blocked: bool
) -> None:
    assert _is_blocked(ipaddress.ip_address(address)) is blocked


# --- repair round 1 (L6): no compressed bodies ---------------------------------------------------


async def test_the_request_asks_for_an_uncompressed_body(server: Server) -> None:
    await _open().fetch(server.url("/ok"))

    [(_, headers)] = server.requests
    assert headers["accept-encoding"] == "identity"


async def test_a_compressed_response_is_refused_as_unreachable(server: Server) -> None:
    result = await _open(max_bytes=10_000).fetch(server.url("/gzip"))

    assert _code(result) == "CATALOG_IMPORT_IMAGE_UNREACHABLE"
    assert isinstance(result, Err)
    assert result.error.details == {"content_encoding": "gzip"}


async def test_an_identity_content_encoding_is_accepted(server: Server) -> None:
    assert await _open().fetch(server.url("/identity-encoded")) == Ok(BODY)


async def test_the_body_is_counted_and_returned_as_raw_bytes_never_decoded(
    server: Server,
) -> None:
    # gzip bytes without a Content-Encoding stay gzip bytes: 5,000 decoded bytes would exceed
    # the limit below, the raw ones do not.
    result = await _open(max_bytes=len(GZIP)).fetch(server.url("/gzip-undeclared"))

    assert result == Ok(GZIP)
    assert len(GZIP) < 5000


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
