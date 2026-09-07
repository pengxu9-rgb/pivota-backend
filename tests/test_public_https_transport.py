import asyncio

import httpx
import pytest

from services.public_https_transport import PublicHTTPSTransport, _BoundedStream


@pytest.mark.asyncio
async def test_redirect_revalidates_dns_and_pins_host_and_tls(monkeypatch):
    seen = []

    async def dns(self, host, port, **kwargs):
        return [(2, 1, 6, '', ('127.0.0.1' if host == 'private.example' else '8.8.8.8', port))]

    async def send(self, request):
        seen.append(request)
        return httpx.Response(302, headers={'location': 'https://private.example/secret'}, stream=httpx.ByteStream(b''))

    monkeypatch.setattr(type(asyncio.get_running_loop()), 'getaddrinfo', dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    async with httpx.AsyncClient(transport=PublicHTTPSTransport(), follow_redirects=True, trust_env=False) as client:
        with pytest.raises(ValueError, match='public IP'):
            await client.get('https://public.example/start')
    assert len(seen) == 1
    assert seen[0].url.host == '8.8.8.8'
    assert seen[0].headers['host'] == 'public.example'
    assert seen[0].extensions['sni_hostname'] == 'public.example'


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://example.com', 'https://user:pass@example.com'])
async def test_insecure_targets_rejected(url):
    async with httpx.AsyncClient(transport=PublicHTTPSTransport()) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(url)


@pytest.mark.asyncio
async def test_response_size_bounded():
    stream = _BoundedStream(httpx.ByteStream(b'12345'), limit=4)
    with pytest.raises(httpx.ReadError, match='size limit'):
        async for _ in stream:
            pass
    await stream.aclose()


@pytest.mark.asyncio
async def test_robots_transport_context_is_scoped_and_reset(monkeypatch):
    from services import crawl_politeness as cp
    from services.official_domain_liveness import probe_host_liveness
    seen = []

    async def deny(*args, **kwargs):
        seen.append(cp.ROBOTS_TRANSPORT_FACTORY.get())
        raise cp.RobotsDisallowed('denied')

    monkeypatch.setattr(cp, 'before_request', deny)
    await probe_host_liveness('example.com', resolver=lambda _: True)
    assert seen == [PublicHTTPSTransport]
    assert cp.ROBOTS_TRANSPORT_FACTORY.get() is None
