"""Public-address-pinned HTTPS transport, including every redirect hop.

Uses the store connector's existing DNS validation + TLS SNI pinning mechanism.
The client still sees the original URL; only the connection address changes.
"""
import httpx
from adapters.magento_adapter import _pinned_https_url, _validate_public_https_target


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
    async def handle_async_request(self, request):
        if request.url.scheme != "https" or request.url.username or request.url.password:
            raise httpx.ConnectError("Liveness target must be public HTTPS")
        hostname, address = await _validate_public_https_target(str(request.url))
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode()
        pinned = httpx.Request(
            request.method, _pinned_https_url(str(request.url), address),
            headers=headers, stream=request.stream,
            extensions={**request.extensions, "sni_hostname": hostname},
        )
        response = await super().handle_async_request(pinned)
        response.stream = _BoundedStream(response.stream)
        return response
