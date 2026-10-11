"""Where an outbound webhook may be sent: public addresses only.

Agents and merchants register their own webhook destination URL, and the delivery services
POST signed events to it and store the first 4000 characters of the answer, which the owner
can read back. Without a check, that is a server-side request forgery primitive: register
`http://10.x.x.x/...` or a name that resolves to one, and the backend makes the request from
inside the VPC and hands back the response.

Two layers, because each one alone has a hole:

1. `check_destination_url` runs when a URL is SAVED. It needs no DNS, so it is cheap and gives
   the owner a clear 400. It requires https, no userinfo, and a host that is not obviously
   internal (an IP literal that is not global, `localhost`, a single-label name, an internal
   suffix). A URL that is already stored is not re-checked on save, so an owner toggling
   `enabled` keeps working; layer 2 still guards every send to it.
2. `PublicDestinationTransport` runs on EVERY SEND. It resolves the host, refuses unless every
   address is public, and then connects to the address it checked, keeping the original Host
   header and TLS SNI. Resolving once and connecting to that address is what defeats DNS
   rebinding (a name that answers public for the check and private for the connect). Redirects
   are never followed by the callers. Response bodies are capped.

A refusal raises `DestinationBlocked`, which the delivery services record as a terminal
failure: retrying a blocked address cannot succeed and only repeats the attempt.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable, List, Optional, Union
from urllib.parse import urlsplit

import httpx

#: Bytes of a destination's answer read before the read fails. The services keep 4000 chars.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024

_INTERNAL_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".corp", ".private",
)
#: NAT64 well-known prefix: a "public" IPv6 address that a NAT64 gateway turns into any IPv4.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Resolver = Callable[[str, int], Awaitable[List[str]]]


class DestinationRejected(ValueError):
    """A URL refused when it is saved. The message is safe to show to its owner."""


class DestinationBlocked(Exception):
    """A send refused because the destination is not a public address. Terminal, not retried."""


def is_public_address(ip: IPAddress) -> bool:
    """Globally routable, and not a v6 wrapper around a v4 address that is not."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64:
            return False
        for embedded in (ip.ipv4_mapped, ip.sixtofour, (ip.teredo or (None, None))[1]):
            if embedded is not None and not is_public_address(embedded):
                return False
    return bool(ip.is_global) and not ip.is_multicast


def _is_internal_name(host: str) -> bool:
    return host == "localhost" or "." not in host or host.endswith(_INTERNAL_SUFFIXES)


def check_destination_url(raw: Optional[str]) -> str:
    """The URL to store, or `DestinationRejected`. No DNS: the send-time transport does that."""
    value = str(raw or "").strip()
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise DestinationRejected("Webhook URL must use https://")
    if "@" in parts.netloc:
        raise DestinationRejected("Webhook URL must not contain credentials")
    try:
        parts.port
    except ValueError as exc:
        raise DestinationRejected("Webhook URL has an invalid port") from exc
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise DestinationRejected("Webhook URL must have a host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _is_internal_name(host):
            raise DestinationRejected("Webhook URL must point to a public host")
        return value
    if not is_public_address(ip):
        raise DestinationRejected("Webhook URL must point to a public address")
    return value


async def _system_resolver(host: str, port: int) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos if info and info[4]})


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, limit: int):
        self._stream, self._limit = stream, limit

    async def __aiter__(self):
        total = 0
        async for chunk in self._stream:
            total += len(chunk)
            if total > self._limit:
                raise httpx.ReadError("Webhook destination response exceeds the size limit")
            yield chunk

    async def aclose(self):
        await self._stream.aclose()


class PublicDestinationTransport(httpx.AsyncHTTPTransport):
    """Connects only to a public address it resolved itself, then pins the connection to it."""

    def __init__(
        self,
        *args,
        resolver: Optional[Resolver] = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._resolver = resolver or _system_resolver
        self._max_response_bytes = max_response_bytes

    async def _public_address(self, host: str, port: int) -> str:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            candidates = [str(literal)]
        elif _is_internal_name(host.rstrip(".").lower()):
            raise DestinationBlocked("destination host is internal")
        else:
            try:
                candidates = await self._resolver(host, port)
            except OSError as exc:
                # A name that does not resolve now may resolve later: transient, so retried.
                raise httpx.ConnectError(f"destination host did not resolve: {type(exc).__name__}") from exc
        if not candidates:
            raise httpx.ConnectError("destination host did not resolve")
        addresses = []
        for candidate in candidates:
            try:
                addresses.append(ipaddress.ip_address(candidate.split("%", 1)[0]))
            except ValueError as exc:
                raise DestinationBlocked("destination resolved to an invalid address") from exc
        if not all(is_public_address(ip) for ip in addresses):
            raise DestinationBlocked("destination resolves to a non-public address")
        return str(sorted(addresses, key=lambda ip: (ip.version, int(ip)))[0])

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise DestinationBlocked("destination scheme is not http(s)")
        if url.userinfo:
            raise DestinationBlocked("destination URL carries credentials")
        port = url.port or (443 if url.scheme == "https" else 80)
        address = await self._public_address(url.host, port)
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        if url.scheme == "https":
            # Certificate checked against the name the owner registered, not the address.
            extensions["sni_hostname"] = url.host
        pinned = httpx.Request(
            request.method, url.copy_with(host=address, port=port),
            headers=headers, stream=request.stream, extensions=extensions,
        )
        response = await super().handle_async_request(pinned)
        response.stream = _BoundedStream(response.stream, self._max_response_bytes)
        return response
