"""An allowlist market the registry does not list allows NO destination.

An empty allowlist allows every domain (backward compatible: a market with no rows yet). That is only safe for a
market the registry lists. A look-alike ('ſg', kept as sent by the ASCII-first key), a three-letter code or an
unlisted code keyed its own empty allowlist and so allowed every domain: the employee seed preview answered
`domain_allowed: true` under 'ſg'. Now it is refused, named (`allowlist_market_unknown`). Absent still keys US.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest


@pytest.fixture
def empty_allowlists(monkeypatch: pytest.MonkeyPatch):
    import services.outbound_links_service as outbound_links_service

    asked: List[str] = []

    async def fake_fetch(cache_market: str) -> List[str]:
        asked.append(cache_market)
        return []

    cache, inflight = outbound_links_service._ALLOWED_DOMAIN_CACHE, outbound_links_service._ALLOWED_DOMAIN_INFLIGHT
    saved = (dict(cache), dict(inflight))
    cache.clear(); inflight.clear()
    monkeypatch.setattr(outbound_links_service, "_fetch_allowed_domains_for_market", fake_fetch)
    try:
        yield outbound_links_service, asked
    finally:
        cache.clear(); cache.update(saved[0]); inflight.clear(); inflight.update(saved[1])


@pytest.mark.parametrize("market", ["ſg", "uſ", "ＳＧ", "USA", "ZZ", "EU-DE", "en-US"])
def test_a_market_the_registry_does_not_list_allows_no_destination(empty_allowlists, market) -> None:
    service, asked = empty_allowlists
    assert service.allowlist_market_refusal(market) == "allowlist_market_unknown"
    allowed = asyncio.run(service._is_domain_allowed(market=market, destination_url="https://shop.example.com/p/1"))
    assert allowed is False
    assert asked == [], "nothing is read for a market the allowlist cannot key"


@pytest.mark.parametrize("market", [None, "", "  ", "US", "us", " SG ", "GB", "JP"])
def test_a_listed_or_absent_market_keeps_the_empty_allowlist_reading(empty_allowlists, market) -> None:
    service, _ = empty_allowlists
    assert service.allowlist_market_refusal(market) is None
    assert asyncio.run(service._is_domain_allowed(market=market, destination_url="https://shop.example.com/p/1")) is True


def _preview(monkeypatch: pytest.MonkeyPatch, market: Any, *, snapshot_fails: bool = False) -> dict:
    import routes.employee_products as ep

    async def fake_resolve(**_kw: Any) -> Any:
        if snapshot_fails:
            raise RuntimeError("store unreachable")
        return SimpleNamespace(canonical_url="https://shop.example.com/p/1", domain="shop.example.com", title="Item",
                               price_amount=10.0, price_currency="USD", availability="in_stock", image_url=None)

    monkeypatch.setattr(ep, "resolve_external_offer", fake_resolve)
    body = ep.PreviewExternalSeedRequest(destination_url="https://shop.example.com/p/1", market=market)
    return asyncio.run(ep.preview_external_seed(body=body, current_user={"role": "admin"}))


@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_the_employee_preview_names_why_a_lookalike_market_allows_no_domain(monkeypatch, empty_allowlists, snapshot_fails) -> None:
    out = _preview(monkeypatch, "ſg", snapshot_fails=snapshot_fails)
    assert out["preview"]["domain_allowed"] is False
    assert out["preview"]["domain_allowed_reason"] == "allowlist_market_unknown"


@pytest.mark.parametrize("market", ["US", None, "SG"])
@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_the_employee_preview_is_unchanged_for_a_listed_or_absent_market(monkeypatch, empty_allowlists, market, snapshot_fails) -> None:
    out = _preview(monkeypatch, market, snapshot_fails=snapshot_fails)
    assert out["preview"]["domain_allowed"] is True
    assert "domain_allowed_reason" not in out["preview"]
