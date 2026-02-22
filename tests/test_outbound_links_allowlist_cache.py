import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_get_allowed_domains_for_market_dedupes_concurrent_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.outbound_links_service as outbound_links_service

    outbound_links_service._ALLOWED_DOMAIN_CACHE.clear()
    outbound_links_service._ALLOWED_DOMAIN_INFLIGHT.clear()

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
