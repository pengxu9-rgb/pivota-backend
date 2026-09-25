"""Public-address-pinned HTTPS transport, including every redirect hop.

Uses the store connector's existing DNS validation + TLS SNI pinning mechanism.
The client still sees the original URL; only the connection address changes.
"""
import time

import httpx
from adapters.magento_adapter import _pinned_https_url, _validate_public_https_target

# The httpx timeout is PER HOP and httpx applies it afresh to every redirect,
# so a chain of `max_redirects` hops multiplies it. official_domain_liveness
# runs 12s per hop with max_redirects=5 — 72 seconds for ONE domain, inside a
# sweep whose run_deadline_seconds is 60. A single host redirecting slowly in a
# loop could therefore eat the entire sweep window and starve every domain
# behind it. This is the bound on the WHOLE chain; callers whose own deadline
# is tighter pass their own.
DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0

# Where the deadline for one chain is carried. httpx's _build_redirect_request
# hands the SAME extensions dict to the redirected request, so stamping it on
# the first hop is what makes the budget cumulative rather than per-hop.
CHAIN_DEADLINE_EXTENSION = "pivota_chain_deadline"

# https on any port but 443 is not a shape any of this transport's callers ask
# for, and an off-port target is a classic way to reach something that is
# public by address and internal by service. Opt in explicitly if you need one.
DEFAULT_ALLOWED_PORTS = (443,)

_TIMEOUT_KEYS = ("connect", "read", "write", "pool")


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit=512 * 1024):
        self.stream, self.limit = stream, limit

    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > self.limit:
                raise httpx.ReadError("Liveness response exceeds size limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PublicHTTPSTransport(httpx.AsyncHTTPTransport):
    def __init__(
        self,
        *args,
        total_timeout=DEFAULT_TOTAL_TIMEOUT_SECONDS,
        allowed_ports=DEFAULT_ALLOWED_PORTS,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.total_timeout = total_timeout
        self.allowed_ports = frozenset(int(p) for p in allowed_ports)

    def _remaining(self, request):
        """Seconds left in this redirect chain's budget, stamping the deadline
        on the first hop. `None` when no total bound is configured."""
        if not self.total_timeout:
            return None
        extensions = request.extensions
        deadline = None
        if isinstance(extensions, dict):
            deadline = extensions.get(CHAIN_DEADLINE_EXTENSION)
        if deadline is None:
            deadline = time.monotonic() + self.total_timeout
            if isinstance(extensions, dict):
                extensions[CHAIN_DEADLINE_EXTENSION] = deadline
        return deadline - time.monotonic()

    @staticmethod
    def _clamped_timeout(extensions, remaining):
        """The per-hop timeout, capped at what is left of the chain budget."""
        timeout = extensions.get("timeout")
        if not isinstance(timeout, dict):
            return {key: remaining for key in _TIMEOUT_KEYS}
        out = dict(timeout)
        for key, value in timeout.items():
            if isinstance(value, (int, float)) and value > remaining:
                out[key] = remaining
        return out

    async def handle_async_request(self, request):
        if request.url.scheme != "https" or request.url.username or request.url.password:
            raise httpx.ConnectError("Liveness target must be public HTTPS")
        port = request.url.port or 443
        if port not in self.allowed_ports:
            raise httpx.ConnectError(
                f"Liveness target port {port} is not allowed"
            )
        remaining = self._remaining(request)
        if remaining is not None and remaining <= 0:
            # Raised, not silently truncated: a chain that ran out of budget
            # has told us nothing, and classify_host_liveness turns a transport
            # error into `unverifiable` — never into a dead domain.
            raise httpx.ConnectTimeout(
                "Liveness redirect chain exceeded its total time budget"
            )
        hostname, address = await _validate_public_https_target(str(request.url))
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode()
        extensions = {**request.extensions, "sni_hostname": hostname}
        if remaining is not None:
            extensions["timeout"] = self._clamped_timeout(extensions, remaining)
        pinned = httpx.Request(
            request.method, _pinned_https_url(str(request.url), address),
            headers=headers, stream=request.stream,
            extensions=extensions,
        )
        response = await super().handle_async_request(pinned)
        response.stream = _BoundedStream(response.stream)
        return response
