import asyncio

import httpx
import pytest

from services.public_https_transport import (
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    PublicHTTPSTransport,
    _BoundedStream,
)


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
    from services.official_domain_liveness import (
        HTTP_TOTAL_TIMEOUT_SECONDS, probe_host_liveness,
    )
    seen = []

    async def deny(*args, **kwargs):
        seen.append(cp.ROBOTS_TRANSPORT_FACTORY.get())
        raise cp.RobotsDisallowed('denied')

    monkeypatch.setattr(cp, 'before_request', deny)
    await probe_host_liveness('example.com', resolver=lambda _: True)
    assert cp.ROBOTS_TRANSPORT_FACTORY.get() is None

    # crawl_politeness calls the factory with NO arguments, so what has to be
    # right is the transport it BUILDS — asserting the factory IS the class
    # said nothing about the budget that transport would carry.
    factory, = seen
    transport = factory()
    assert isinstance(transport, PublicHTTPSTransport)
    # The robots.txt fetch happens FIRST and shares the probe's wall clock.
    # Handed the bare class it took the transport's own 30s default while this
    # caller had chosen 25s for the apex GET, so the robots hop alone could
    # outlast the budget the whole probe is bounded by — and that budget is
    # what stops one slow host starving the 100 domains behind it in a sweep.
    assert transport.total_timeout == HTTP_TOTAL_TIMEOUT_SECONDS
    # The two numbers must actually differ, or this test proves nothing.
    assert HTTP_TOTAL_TIMEOUT_SECONDS != DEFAULT_TOTAL_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# The per-hop timeout is not a bound on the CHAIN, and https is not a bound on
# the PORT.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('url,allowed,rejected', [
    ('https://example.com:8443/x', (443,), True),
    ('https://example.com:80/x', (443,), True),
    ('https://example.com/x', (443,), False),
    ('https://example.com:443/x', (443,), False),
    ('https://example.com:8443/x', (443, 8443), False),
])
async def test_only_port_443_unless_the_caller_opts_in(monkeypatch, url, allowed, rejected):
    from services.public_https_transport import PublicHTTPSTransport

    async def dns(self, host, port, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', port))]

    async def send(self, request):
        return httpx.Response(200, stream=httpx.ByteStream(b''))

    monkeypatch.setattr(type(asyncio.get_running_loop()), 'getaddrinfo', dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    transport = PublicHTTPSTransport(allowed_ports=allowed)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        if rejected:
            with pytest.raises(httpx.ConnectError, match='is not allowed'):
                await client.get(url)
        else:
            assert (await client.get(url)).status_code == 200


@pytest.mark.asyncio
async def test_the_redirect_chain_is_bounded_in_total_not_only_per_hop(monkeypatch):
    """httpx applies its timeout AFRESH to every hop, so 12s x (5 redirects +
    1) is 72 seconds for one domain — inside a sweep the scheduler gives 60.
    The budget has to span the chain."""
    from services.public_https_transport import PublicHTTPSTransport

    clock = {'now': 0.0}
    hops = []

    async def dns(self, host, port, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', port))]

    async def send(self, request):
        hops.append(request.url.path)
        clock['now'] += 12.0  # one per-hop timeout's worth of wall clock
        return httpx.Response(
            302, headers={'location': f'https://public.example/hop{len(hops)}'},
            stream=httpx.ByteStream(b''),
        )

    monkeypatch.setattr(type(asyncio.get_running_loop()), 'getaddrinfo', dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    monkeypatch.setattr(
        'services.public_https_transport.time.monotonic', lambda: clock['now'],
    )
    transport = PublicHTTPSTransport(total_timeout=25.0)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True,
                                 max_redirects=5, trust_env=False) as client:
        with pytest.raises(httpx.ConnectTimeout, match='total time budget'):
            await client.get('https://public.example/start')
    # 25s of budget at 12s a hop: the fourth hop is refused, and the third
    # ran with its timeout clamped to the 1s left. Without a total bound all
    # six hops would have run, for 72 seconds.
    assert len(hops) == 3


@pytest.mark.asyncio
async def test_the_chain_budget_clamps_the_per_hop_timeout(monkeypatch):
    """A hop must not be allowed to overshoot the chain deadline on its own."""
    from services.public_https_transport import PublicHTTPSTransport

    clock = {'now': 0.0}
    seen = []

    async def dns(self, host, port, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', port))]

    async def send(self, request):
        seen.append(request.extensions.get('timeout'))
        clock['now'] += 9.0
        return httpx.Response(
            302, headers={'location': 'https://public.example/next'},
            stream=httpx.ByteStream(b''),
        )

    monkeypatch.setattr(type(asyncio.get_running_loop()), 'getaddrinfo', dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    monkeypatch.setattr(
        'services.public_https_transport.time.monotonic', lambda: clock['now'],
    )
    transport = PublicHTTPSTransport(total_timeout=10.0)
    async with httpx.AsyncClient(transport=transport, timeout=12.0,
                                 follow_redirects=True, max_redirects=5,
                                 trust_env=False) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get('https://public.example/start')
    # First hop: the client asked for 12s, the chain only had 10 left.
    assert seen[0]['read'] == 10.0
    assert seen[0]['connect'] == 10.0


@pytest.mark.asyncio
async def test_a_fresh_request_gets_a_fresh_budget(monkeypatch):
    """The transport is reused for every domain in a sweep; the budget is per
    CHAIN, so one slow domain must not poison the next."""
    from services.public_https_transport import PublicHTTPSTransport

    clock = {'now': 0.0}

    async def dns(self, host, port, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', port))]

    async def send(self, request):
        clock['now'] += 24.0
        return httpx.Response(200, stream=httpx.ByteStream(b''))

    monkeypatch.setattr(type(asyncio.get_running_loop()), 'getaddrinfo', dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    monkeypatch.setattr(
        'services.public_https_transport.time.monotonic', lambda: clock['now'],
    )
    transport = PublicHTTPSTransport(total_timeout=25.0)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        for _ in range(4):
            assert (await client.get('https://public.example/x')).status_code == 200


def test_the_liveness_sweep_declares_a_chain_budget_under_its_run_deadline():
    from services import official_domain_liveness as odl

    hops = 6  # max_redirects=5, plus the original request
    assert odl.HTTP_TOTAL_TIMEOUT_SECONDS < odl.HTTP_TIMEOUT_SECONDS * hops
    # The scheduler tick calls refresh_official_domain_liveness with
    # run_deadline_seconds=60; one domain must not be able to spend it all.
    assert odl.HTTP_TOTAL_TIMEOUT_SECONDS < 60
