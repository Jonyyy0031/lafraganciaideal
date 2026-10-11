"""Downloads the image URLs of an import file, hardened against SSRF.

The URLs come from a spreadsheet, so:

- every resolved address must be public (`is_global`, not multicast), unless
  `allow_private_hosts` (tests and local QA only);
- the request connects to the checked IP itself, so DNS cannot change the address between the
  check and the connection (rebinding). It sends the original `Host` header and, for https, the
  original name as TLS SNI (the `sni_hostname` extension of httpcore), against which the
  certificate is verified;
- redirects are followed by hand, at most 3, and each hop is checked again;
- proxies from the environment are ignored, the body is cut off at `max_bytes`, and the whole
  fetch has 30 seconds.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

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
                    url = urljoin(url, redirect.location)
                case Ok(bytes() as body):
                    return Ok(body)
                case Err(error):
                    return Err(error)
        return Err(ImportImageUnreachable(details={"redirects": MAX_REDIRECTS}))

    async def _fetch_once(self, url: str) -> Result[bytes | _Redirect, DomainError]:
        parts = urlsplit(url)
        host = parts.hostname
        try:
            port = parts.port
        except ValueError:
            return Err(ImportImageUrlInvalid())
        if (
            parts.scheme not in _DEFAULT_PORTS
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            return Err(ImportImageUrlInvalid())
        default_port = _DEFAULT_PORTS[parts.scheme]
        port = port or default_port

        match await self._resolve(host, port):
            case Err(refused):
                return Err(refused)
            case Ok(addresses):
                pass

        named_host = f"[{host}]" if ":" in host else host
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
            except httpx.HTTPError:
                return Err(ImportImageUnreachable())
        return Err(ImportImageUnreachable())

    async def _resolve(
        self, host: str, port: int
    ) -> Result[list[ipaddress.IPv4Address | ipaddress.IPv6Address], DomainError]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return Err(ImportImageUnreachable())
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
        if not addresses:
            return Err(ImportImageUnreachable())
        if not self._allow_private_hosts and any(
            not address.is_global or address.is_multicast for address in addresses
        ):
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
                "GET", target, headers={"Host": host_header}, extensions=extensions
            ) as response,
        ):
            location = response.headers.get("location")
            if response.status_code in _REDIRECTS and location:
                return Ok(_Redirect(location))
            if response.status_code != 200:
                return Err(ImportImageUnreachable(details={"status": response.status_code}))
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                return Err(too_large)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_bytes:
                    return Err(too_large)
                chunks.append(chunk)
            return Ok(b"".join(chunks))
