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
   are never followed by the callers. Response bodies are capped. Worst case for one send:
   resolve (connect timeout) + up to three connects + the read, about 50 s at 10 s timeouts.

PUBLIC IS NOT THE SAME AS OUTSIDE. The senders run with all-traffic VPC egress, under which a
call to one of OUR OWN Cloud Run services (`*.run.app`, public anycast addresses) arrives as
INTERNAL ingress: past the load balancer and Cloud Armor, and on services that accept internal
traffic without auth. So names under the suffixes in `WEBHOOK_BLOCKED_HOST_SUFFIXES` (default
`.run.app`) are refused at both layers, whatever they resolve to. The front end routes by Host and
SNI, which the transport keeps as the registered name, so a foreign name pointed at the same
addresses cannot reach our services.

A refusal raises `DestinationBlocked`, which the delivery services record as a terminal
failure: retrying a blocked address cannot succeed and only repeats the attempt.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from typing import Awaitable, Callable, List, Optional, Union
from urllib.parse import urlsplit

import httpx

#: Bytes of a destination's answer read before the read fails. The services keep 4000 chars.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024

_INTERNAL_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".corp", ".private",
)
#: v6 ranges that wrap or reach an IPv4 address: NAT64 (well-known and local-use) and the
#: deprecated IPv4-compatible block. The stdlib calls some of these global.
_V6_WRAPPERS = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "::/96"))
_BLOCKED_SUFFIXES_ENV = "WEBHOOK_BLOCKED_HOST_SUFFIXES"
_DEFAULT_BLOCKED_SUFFIXES = (".run.app",)
#: What a resolvable host (punycode name, IPv4, or bracket-less IPv6 literal) is made of.
_HOST_CHARS_RE = re.compile(r"^[a-z0-9.:-]+\Z")
#: Addresses of one name tried, in order, when connecting fails (a CDN with one node down).
_MAX_ADDRESS_ATTEMPTS = 3
#: Seconds to wait for DNS when the request carries no connect timeout.
DEFAULT_RESOLVE_TIMEOUT_SECONDS = 10.0

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Resolver = Callable[[str, int], Awaitable[List[str]]]


class DestinationRejected(ValueError):
    """A URL refused when it is saved. The message is safe to show to its owner."""


class DestinationBlocked(Exception):
    """A send refused because the destination is not a public address. Terminal, not retried."""


def is_public_address(ip: IPAddress) -> bool:
    """Globally routable, and not a v6 wrapper around a v4 address that is not."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local or any(ip in net for net in _V6_WRAPPERS):
            return False
        for embedded in (ip.ipv4_mapped, ip.sixtofour, (ip.teredo or (None, None))[1]):
            if embedded is not None and not is_public_address(embedded):
                return False
    return bool(ip.is_global) and not ip.is_multicast


def _blocked_suffixes() -> tuple:
    """The configured suffixes, ALWAYS including the defaults: an empty or blank value (a Cloud Run
    env entry with no value) must not silently reopen our own services. Extra suffixes add."""
    raw = os.getenv(_BLOCKED_SUFFIXES_ENV) or ""
    extra = tuple("." + p.strip().lower().lstrip(".") for p in raw.split(",") if p.strip().strip("."))
    return _DEFAULT_BLOCKED_SUFFIXES + tuple(x for x in extra if x not in _DEFAULT_BLOCKED_SUFFIXES)


def _is_internal_name(host: str) -> bool:
    host = host.rstrip(".").lower()
    blocked = _blocked_suffixes()
    return (
        host == "localhost"
        or "." not in host
        or host.endswith(_INTERNAL_SUFFIXES)
        or any(host == b.lstrip(".") or host.endswith(b) for b in blocked)
    )


def check_destination_url(raw: Optional[str]) -> str:
    """The URL to store, or `DestinationRejected`. No DNS: the send-time transport does that.

    Judged as httpx will parse it at send time (so a host the two parsers read differently, or a
    URL httpx refuses, is caught here and not as six failed retries later)."""
    value = str(raw or "").strip()
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise DestinationRejected("Webhook URL must use https://")
    if "@" in parts.netloc:
        raise DestinationRejected("Webhook URL must not contain credentials")
    try:
        parts.port
        url = httpx.URL(value)
    except (ValueError, httpx.InvalidURL) as exc:
        raise DestinationRejected("Webhook URL is not a valid URL") from exc
    if url.port == 0:
        raise DestinationRejected("Webhook URL has an invalid port")
    host = url.raw_host.decode("ascii").rstrip(".").lower()
    if not host:
        raise DestinationRejected("Webhook URL must have a host")
    if not _HOST_CHARS_RE.match(host):
        # A host no resolver can look up (`%`, a space, a backslash) is six failed sends later.
        raise DestinationRejected("Webhook URL has an invalid host")
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
    """Reads at most `limit` bytes of the answer and then stops, quietly: the status code is what
    decides a delivery, and the services keep only the first 4000 characters of the body. (Raising
    here would turn a 200 with a long HTML page into six retries.)"""

    def __init__(self, stream: httpx.AsyncByteStream, limit: int):
        self._stream, self._limit = stream, limit

    async def __aiter__(self):
        remaining = self._limit
        async for chunk in self._stream:
            if len(chunk) >= remaining:
                if remaining:
                    yield chunk[:remaining]
                return
            remaining -= len(chunk)
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

    async def _public_addresses(self, host: str, port: int, timeout: Optional[float]) -> List[str]:
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
                # Bounded: before this transport, httpcore resolved inside its connect timeout.
                candidates = await asyncio.wait_for(
                    self._resolver(host, port), timeout or DEFAULT_RESOLVE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise httpx.ConnectTimeout("destination host did not resolve in time") from exc
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
        # IPv4 first (egress NAT is IPv4), then in order; tried one after another on connect errors.
        return [str(ip) for ip in sorted(set(addresses), key=lambda ip: (ip.version, int(ip)))]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise DestinationBlocked("destination scheme is not http(s)")
        if url.userinfo:
            raise DestinationBlocked("destination URL carries credentials")
        if url.port == 0:
            raise DestinationBlocked("destination port is invalid")
        port = url.port or (443 if url.scheme == "https" else 80)
        timeout = request.extensions.get("timeout") if isinstance(request.extensions.get("timeout"), dict) else {}
        # The ASCII (punycode) host, exactly as the Host header carries it: resolving, the suffix
        # check and TLS must all judge the same name (`url.host` is unicode, and Python's IDNA
        # 2003 codec maps e.g. `faß.de` to a DIFFERENT domain than httpx's `xn--fa-hia.de`).
        host = url.raw_host.decode("ascii")
        addresses = await self._public_addresses(host, port, timeout.get("connect"))
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        if url.scheme == "https":
            # Certificate checked against the name the owner registered, not the address.
            extensions["sni_hostname"] = host.rstrip(".")
        body = await request.aread()  # replayable across address attempts
        last_error: Optional[Exception] = None
        for address in addresses[:_MAX_ADDRESS_ATTEMPTS]:
            pinned = httpx.Request(
                request.method, url.copy_with(host=address, port=port),
                headers=headers, content=body, extensions=extensions,
            )
            try:
                response = await super().handle_async_request(pinned)
            except httpx.ConnectError as exc:
                last_error = exc  # nothing was sent to this address: the next one is safe to try
                continue
            response.stream = _BoundedStream(response.stream, self._max_response_bytes)
            return response
        assert last_error is not None
        raise last_error
