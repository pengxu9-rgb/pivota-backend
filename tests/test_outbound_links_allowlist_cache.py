import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def isolated_allowlist_cache():
    """Run with an empty market allowlist cache, and put the original back after.

    The cache is a module-level dict with a 5-minute TTL. Without this, the
    ``US -> ["example.com"]`` entry the test below writes outlives it, and any
    later test in the same process that resolves a US outbound link sees an
    allowlist that rejects its merchant's domain (tests/test_offers_resolve.py
    lost 58 tests that way when run after this file).

    The dicts are emptied and refilled in place, never rebound: callers that
    imported them by name must keep seeing the same objects.
    """
    import services.outbound_links_service as outbound_links_service

    cache = outbound_links_service._ALLOWED_DOMAIN_CACHE
    inflight = outbound_links_service._ALLOWED_DOMAIN_INFLIGHT
    saved_cache = dict(cache)
    saved_inflight = dict(inflight)
    cache.clear()
    inflight.clear()
    try:
        yield outbound_links_service
    finally:
        cache.clear()
        cache.update(saved_cache)
        inflight.clear()
        inflight.update(saved_inflight)


@pytest.mark.asyncio
async def test_get_allowed_domains_for_market_dedupes_concurrent_fetches(
    monkeypatch: pytest.MonkeyPatch,
    isolated_allowlist_cache,
) -> None:
    outbound_links_service = isolated_allowlist_cache

    fetch_mock = AsyncMock(side_effect=[[{"domain": "example.com"}]])
    monkeypatch.setattr(outbound_links_service.database, "fetch_all", fetch_mock)

    results = await asyncio.gather(
        *[
            outbound_links_service.get_allowed_domains_for_market(market="US")
            for _ in range(6)
        ]
    )

    assert fetch_mock.await_count == 1
    assert all(r == ["example.com"] for r in results)


def test_is_destination_domain_allowed_honors_allowlist() -> None:
    import services.outbound_links_service as outbound_links_service

    assert outbound_links_service.is_destination_domain_allowed(
        destination_url="https://shop.example.com/p/1",
        allowed_domains=["example.com"],
    )
    assert not outbound_links_service.is_destination_domain_allowed(
        destination_url="https://evil.com/p/1",
        allowed_domains=["example.com"],
    )
    assert outbound_links_service.is_destination_domain_allowed(
        destination_url="https://any-domain.com/p/1",
        allowed_domains=[],
    )
