"""Downloads the image URLs of an import file, hardened against SSRF.

The URLs come from a spreadsheet, so:

- every resolved address must be public (`is_global`, not multicast, not IPv4-compatible IPv6
  or NAT64; an IPv4-mapped address is judged by its IPv4 address), unless
  `allow_private_hosts` (tests and local QA only);
- the request connects to the checked IP itself, so DNS cannot change the address between the
  check and the connection (rebinding). It sends the original `Host` header and, for https, the
  original name as TLS SNI (the `sni_hostname` extension of httpcore), against which the
  certificate is verified. An internationalized host is sent in its IDNA (ASCII) form;
- redirects are followed by hand, at most 3, and each hop is checked again;
- proxies from the environment are ignored, compressed bodies are refused
  (`Accept-Encoding: identity`), the raw body is cut off at `max_bytes`, and the whole fetch
  has 30 seconds;
- a bad URL or host never raises: it becomes `IMAGE_URL_INVALID` or `IMAGE_UNREACHABLE`.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

from fragancia_api.modules.catalog.domain.errors import (
    ImportImageHostBlocked,
    ImportImageUnreachable,
    ImportImageUrlInvalid,
    PhotoTooLarge,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 30
_REDIRECTS = frozenset((301, 302, 303, 307, 308))
_DEFAULT_PORTS = {"http": 80, "https": 443}
# IPv6 ranges `is_global` lets through but that can reach IPv4 hosts (IPv4-compatible, NAT64).
_BLOCKED_IPV6 = (
    ipaddress.IPv6Network("::/96"),
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_blocked(address.ipv4_mapped)
        if any(address in network for network in _BLOCKED_IPV6):
            return True
    return not address.is_global or address.is_multicast


def _ascii_host(host: str) -> str | None:
    """The host as sent on the wire: IDNA for a name, as is for an IP literal; None if invalid."""
    if ":" in host:  # an IPv6 literal (urlsplit already checked its brackets)
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


class _Redirect:
    def __init__(self, location: str) -> None:
        self.location = location


class HttpImageDownloader:
    def __init__(self, *, max_bytes: int, allow_private_hosts: bool) -> None:
        self._max_bytes = max_bytes
        self._allow_private_hosts = allow_private_hosts

    async def fetch(self, url: str) -> Result[bytes, DomainError]:
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                return await self._follow(url)
        except TimeoutError:
            return Err(ImportImageUnreachable())

    async def _follow(self, url: str) -> Result[bytes, DomainError]:
        for _ in range(MAX_REDIRECTS + 1):
            match await self._fetch_once(url):
                case Ok(_Redirect() as redirect):
                    try:
                        url = urljoin(url, redirect.location)
                    except ValueError:  # e.g. `Location: http://[x/`
                        return Err(ImportImageUrlInvalid())
                case Ok(bytes() as body):
                    return Ok(body)
                case Err(error):
                    return Err(error)
        return Err(ImportImageUnreachable(details={"redirects": MAX_REDIRECTS}))

    @staticmethod
    def _split(url: str) -> tuple[SplitResult, str, int] | None:
        """The URL parts, the ASCII host and the port; None when the URL is not usable."""
        try:
            parts = urlsplit(url)
            host = parts.hostname
            port = parts.port
        except ValueError:  # a bad IPv6 literal or port
            return None
        if (
            parts.scheme not in _DEFAULT_PORTS
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        ascii_host = _ascii_host(host)
        if not ascii_host:
            return None
        return parts, ascii_host, port or _DEFAULT_PORTS[parts.scheme]

    async def _fetch_once(self, url: str) -> Result[bytes | _Redirect, DomainError]:
        split = self._split(url)
        if split is None:
            return Err(ImportImageUrlInvalid())
        parts, host, port = split

        match await self._resolve(host, port):
            case Err(refused):
                return Err(refused)
            case Ok(addresses):
                pass

        named_host = f"[{host}]" if ":" in host else host
        default_port = _DEFAULT_PORTS[parts.scheme]
        host_header = named_host if port == default_port else f"{named_host}:{port}"
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        # Every checked address in resolver order, until one accepts the connection.
        for address in addresses:
            ip_host = f"[{address}]" if address.version == 6 else str(address)
            target = urlunsplit(
                (parts.scheme, f"{ip_host}:{port}", parts.path or "/", parts.query, "")
            )
            try:
                return await self._get(target, host_header, extensions)
            except httpx.ConnectError:
                continue
            except httpx.InvalidURL:
                return Err(ImportImageUrlInvalid())
            except httpx.HTTPError:
                return Err(ImportImageUnreachable())
        return Err(ImportImageUnreachable())

    async def _resolve(
        self, host: str, port: int
    ) -> Result[list[ipaddress.IPv4Address | ipaddress.IPv6Address], DomainError]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError, UnicodeError:  # socket.gaierror is an OSError
            return Err(ImportImageUnreachable())
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
        if not addresses:
            return Err(ImportImageUnreachable())
        if not self._allow_private_hosts and any(_is_blocked(address) for address in addresses):
            return Err(ImportImageHostBlocked())
        return Ok(list(dict.fromkeys(addresses)))  # unique, in resolver order

    async def _get(
        self, target: str, host_header: str, extensions: dict[str, str]
    ) -> Result[bytes | _Redirect, DomainError]:
        too_large = PhotoTooLarge(details={"max_bytes": self._max_bytes})
        async with (
            httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(10.0, read=20.0),
                trust_env=False,
            ) as client,
            client.stream(
                "GET",
                target,
                headers={"Host": host_header, "Accept-Encoding": "identity"},
                extensions=extensions,
            ) as response,
        ):
            location = response.headers.get("location")
            if response.status_code in _REDIRECTS and location:
                return Ok(_Redirect(location))
            if response.status_code != 200:
                return Err(ImportImageUnreachable(details={"status": response.status_code}))
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding != "identity":  # a compressed body could expand past `max_bytes`
                return Err(ImportImageUnreachable(details={"content_encoding": encoding}))
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                return Err(too_large)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_raw():  # the bytes as sent, never decoded
                total += len(chunk)
                if total > self._max_bytes:
                    return Err(too_large)
                chunks.append(chunk)
            return Ok(b"".join(chunks))
