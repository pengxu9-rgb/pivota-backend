"""`serving_market` on GET /agent/v1/products/search (2026-10-09, option (A) for the PIVOTA-Agent
discovery feed's buyer-market fallback).

TWO MARKETS, TWO MEANINGS. `market` is the storage PARTITION, exactly as before. `serving_market`
is the market the BUYER is served in: it decides the expected currency and rides down to every
seed fetch (`fetch_external_seed_rows(serving_market=...)`), while the partition read stays the
deployment's. Every SGD seed is filed under the 'US' partition, so `market=SG` bound an empty
partition and the gateway's fallback got nothing; `serving_market=SG` reads the US partition and
keeps the SGD rows. A request naming no serving market is byte-identical to before.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    try:
        from main import app
    except Exception:  # pragma: no cover - environment-specific
        pytest.skip("main app import requires a clean repo without duplicate _tmp order_routes.py")
    return TestClient(app)


def test_normalize_serving_market_param_admits_one_priceable_iso2_code_only() -> None:
    import routes.agent_api as agent_api_module

    norm = agent_api_module._normalize_serving_market_param
    assert norm("SG") == "SG"
    assert norm(" sg ") == "SG"
    assert norm("jp") == "JP"
    assert norm("US") == "US"
    # A locale, a list, a three-letter code, junk, nothing: no claim about the buyer.
    for raw in ("en-US", "US,SG", "USA", "", None, 7, "S", "SGP"):
        assert norm(raw) is None, raw
    # Well-formed but nothing is priced for it: ignored, never defaulted.
    assert norm("ZZ") is None


def test_cache_key_appends_a_serving_component_only_when_named() -> None:
    import routes.agent_api as agent_api_module

    build = agent_api_module._build_external_seed_cache_key
    common: Dict[str, Any] = dict(
        query="sunscreen",
        market="US",
        strategy="legacy",
        surface="all",
        scope="default",
        limit=24,
        page_offset=0,
    )
    plain = build(**common)
    assert plain == build(**common, serving_market=None)
    assert plain == build(**common, serving_market="")
    served = build(**common, serving_market="sg")
    assert served == f"{plain}|serving:SG"
    assert build(**common, serving_market="JP") != served
    # The unnamed-market split keeps its own place, and the serving component stays last.
    unnamed = build(**common, request_market_named=False)
    assert unnamed.endswith("|market_unnamed")
    assert build(**common, request_market_named=False, serving_market="SG") == f"{unnamed}|serving:SG"


def _silent_database(agent_api_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_all(query: Any, values: Any = None) -> List[Dict[str, Any]]:
        return []

    async def fake_fetch_one(query: Any, values: Any = None) -> None:
        return None

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module.database, "fetch_one", fake_fetch_one, raising=False)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))


def _search(client: TestClient, **params: Any) -> Any:
    return client.get(
        "/agent/v1/products/search",
        params={"query": "sunscreen", "limit": 10, "offset": 0, "search_all_merchants": "true", **params},
        headers={"X-API-Key": "test-api-key"},
    )


def test_route_threads_serving_market_to_the_seed_loader_and_keeps_market_as_the_partition(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    _silent_database(agent_api_module, monkeypatch)
    seen: List[Dict[str, Any]] = []

    async def fake_loader(**kwargs: Any) -> List[Dict[str, Any]]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_with_cache", fake_loader)
    currencies: List[Any] = []
    real_expected = agent_api_module.expected_currency_for_market

    def spy_expected(value: Any) -> Any:
        currencies.append(value)
        return real_expected(value)

    monkeypatch.setattr(agent_api_module, "expected_currency_for_market", spy_expected)

    # serving_market alone: the partition stays unnamed (the deployment's), the buyer is SG.
    response = _search(client, serving_market="sg")
    assert response.status_code == 200, response.text
    assert seen, "the seed loader was not reached"
    assert seen[-1]["market"] is None
    assert seen[-1]["serving_market"] == "SG"
    # The LAST expected-currency call is the route's own (the validator probes the code first).
    assert currencies[-1] == "SG"

    # market alone: byte-identical to before -- no serving market reaches the loader.
    seen.clear(); currencies.clear()
    response = _search(client, market="US")
    assert response.status_code == 200, response.text
    assert seen[-1]["market"] == "US"
    assert seen[-1]["serving_market"] is None
    assert currencies[-1] == "US"

    # both: the partition is `market`, the buyer is `serving_market`, the currency follows the buyer.
    seen.clear(); currencies.clear()
    response = _search(client, market="US", serving_market="SG")
    assert response.status_code == 200, response.text
    assert seen[-1]["market"] == "US"
    assert seen[-1]["serving_market"] == "SG"
    assert currencies[-1] == "SG"

    # unpriceable or malformed: ignored, the request reads as if it named none.
    for junk in ("ZZ", "USA", "en-US"):
        seen.clear(); currencies.clear()
        response = _search(client, serving_market=junk)
        assert response.status_code == 200, response.text
        assert seen[-1]["serving_market"] is None, junk
        assert currencies[-1] is None, junk


@pytest.mark.asyncio
async def test_inner_search_passes_serving_market_to_every_seed_fetch_and_keeps_the_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    calls: List[Dict[str, Any]] = []

    async def fake_fetch(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {"rows": [], "total_count": 0, "query_timeout": False}

    monkeypatch.setattr(agent_api_module, "fetch_external_seed_rows", fake_fetch)

    products = await agent_api_module._load_external_seed_products_for_search(
        req=_Req(),
        query="sunscreen",
        market="US",
        limit=10,
        page_offset=0,
        serving_market="SG",
    )
    assert products == []
    assert calls, "no seed fetch ran"
    for call in calls:
        assert call["market"] == "US", call
        assert call["serving_market"] == "SG", call

    # Absent: exactly as before -- the fetch is told nothing about a serving market.
    calls.clear()
    await agent_api_module._load_external_seed_products_for_search(
        req=_Req(), query="sunscreen", market="US", limit=10, page_offset=0
    )
    assert calls and all(call.get("serving_market") is None for call in calls)


@pytest.mark.asyncio
async def test_cached_loader_keeps_request_market_raw_so_a_serving_market_only_request_stays_unobserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    seen: List[Dict[str, Any]] = []

    async def fake_inner(**kwargs: Any) -> List[Dict[str, Any]]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", fake_inner)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_FIRST_SCREEN_ONLY", True, raising=False)

    # Page 2 bypasses the cache and calls the inner search directly: the simplest path to the kwargs.
    await agent_api_module._load_external_seed_products_with_cache(
        req=_Req(),
        query="sunscreen",
        market=None,
        query_semantic_class=None,
        limit=10,
        build_budget_ms=100,
        build_concurrency=1,
        include_seed_data_text_match=False,
        normalized_seed_strategy="legacy",
        normalized_catalog_surface="all",
        page_offset=10,
        serving_market="SG",
    )
    assert seen, "the inner search was not reached"
    # The partition read is the deployment's default; the serving market rides beside it; and the
    # RAW request market stays None -- the minted token's market would be the seed ROW's (US), and
    # stamping that observed from a serving-market-only request would hand the purchasability
    # gate a wrong market for an SG buyer. Unobserved, exactly as a market-less request.
    assert seen[-1]["market"] == agent_api_module.DEFAULT_EXTERNAL_SEED_MARKET
    assert seen[-1]["serving_market"] == "SG"
    assert seen[-1]["request_market"] is None
    # And with a partition named too, the raw market is that partition, never the serving market.
    seen.clear()
    await agent_api_module._load_external_seed_products_with_cache(
        req=_Req(), query="sunscreen", market="us", query_semantic_class=None, limit=10, build_budget_ms=100,
        build_concurrency=1, include_seed_data_text_match=False, normalized_seed_strategy="legacy",
        normalized_catalog_surface="all", page_offset=10, serving_market="SG",
    )
    assert seen[-1]["request_market"] == "us"
    assert seen[-1]["serving_market"] == "SG"


def _seed_row() -> Dict[str, Any]:
    return {
        "id": "eps_sg_1",
        "external_product_id": "ext_sg_1",
        "market": "US",  # every SGD seed is filed under the US partition
        "tool": "shopify",
        "status": "active",
        "title": "SG Sunscreen",
        "canonical_url": "https://cocomo.sg/products/sunscreen",
        "destination_url": "https://cocomo.sg/products/sunscreen",
        "seed_data": {"price": 31, "price_currency": "SGD", "currency": "SGD"},
        "price": 31,
        "price_currency": "SGD",
    }


class _Req:
    query_params: Dict[str, str] = {}
    headers: Dict[str, str] = {}
    state = type("S", (), {})()
    base_url = "https://api.test"


def _stub_referral_gates(agent_api_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_api_module, "should_block_external_referral_runtime", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(agent_api_module, "external_referral_live_verification_reasons", lambda status: [])
    monkeypatch.setattr(agent_api_module, "get_allowed_domains_for_market", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *a, **k: True, raising=False)


def _decode_minted_token(product: Dict[str, Any]) -> Dict[str, Any]:
    from urllib.parse import parse_qs, urlparse

    from services.outbound_links_service import parse_and_verify_redirect_token

    url = str(product.get("external_redirect_url") or product.get("affiliate_url") or "")
    token = parse_qs(urlparse(url).query).get("token", [""])[0]
    assert token, f"no /r token on the minted product: {url}"
    return parse_and_verify_redirect_token(token)


@pytest.mark.asyncio
async def test_the_minted_token_of_a_serving_market_only_request_is_unobserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE P1 OF THE REVIEW, through the real inner search and the real producer: the token's
    market is the seed ROW's ('US' for an SGD seed), so a request that named only a serving
    market must leave it UNOBSERVED -- a token saying 'US, observed' would send the warm lane to
    the US fact for an SG buyer."""
    import routes.agent_api as agent_api_module

    _stub_referral_gates(agent_api_module, monkeypatch)
    monkeypatch.setattr(
        agent_api_module, "fetch_external_seed_rows",
        AsyncMock(return_value={"rows": [_seed_row()], "total_count": 1, "query_timeout": False}),
    )
    products = await agent_api_module._load_external_seed_products_for_search(
        req=_Req(), query="sunscreen", market=None, limit=10, page_offset=0, serving_market="SG",
    )
    assert len(products) == 1, products
    token = _decode_minted_token(products[0])
    assert token["market"] == "US"
    assert agent_api_module.TOKEN_MARKET_OBSERVED_KEY not in token, token

    # A request that NAMED the partition stamps it observed, exactly as before this change.
    products = await agent_api_module._load_external_seed_products_for_search(
        req=_Req(), query="sunscreen", market="US", limit=10, page_offset=0, request_market="US", serving_market="SG",
    )
    token = _decode_minted_token(products[0])
    assert token["market"] == "US"
    assert token.get(agent_api_module.TOKEN_MARKET_OBSERVED_KEY) is True


@pytest.mark.asyncio
async def test_the_seed_cache_keeps_an_sg_buyers_page_apart_from_the_us_buyers(monkeypatch: pytest.MonkeyPatch) -> None:
    """M6: the loader's key call must carry serving_market, or a US page out of the shared
    partition is served to an SG buyer (and the other way round)."""
    import routes.agent_api as agent_api_module

    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE.clear()
    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.clear()
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_FIRST_SCREEN_ONLY", False, raising=False)
    calls: List[Dict[str, Any]] = []

    async def fake_inner(**kwargs: Any) -> List[Dict[str, Any]]:
        calls.append(kwargs)
        currency = "SGD" if kwargs.get("serving_market") == "SG" else "USD"
        return [{"merchant_id": "external_seed", "product_id": f"p_{currency}", "price": 10, "currency": currency}]

    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", fake_inner)
    common = dict(
        req=_Req(), query="sunscreen", query_semantic_class=None, limit=10, build_budget_ms=100, build_concurrency=1,
        include_seed_data_text_match=False, normalized_seed_strategy="legacy", normalized_catalog_surface="all", page_offset=0,
    )
    us = await agent_api_module._load_external_seed_products_with_cache(market=None, **common)
    assert [p["currency"] for p in us] == ["USD"]
    sg = await agent_api_module._load_external_seed_products_with_cache(market=None, serving_market="SG", **common)
    assert [p["currency"] for p in sg] == ["SGD"], "the SG buyer was served the US buyer's cached page"
    assert len(calls) == 2
    us_again = await agent_api_module._load_external_seed_products_with_cache(market=None, **common)
    assert [p["currency"] for p in us_again] == ["USD"]
    sg_again = await agent_api_module._load_external_seed_products_with_cache(market=None, serving_market="SG", **common)
    assert [p["currency"] for p in sg_again] == ["SGD"]
    assert len(calls) == 2, "both pages are cached, each under its own entry"


@pytest.mark.asyncio
async def test_the_async_cache_refresh_carries_the_serving_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5: a refresh that dropped serving_market would refill an SG buyer's entry with US rows."""
    import asyncio

    import routes.agent_api as agent_api_module

    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.clear()
    seen: List[Dict[str, Any]] = []

    async def fake_inner(**kwargs: Any) -> List[Dict[str, Any]]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", fake_inner)
    scheduled = agent_api_module._schedule_external_seed_cache_refresh(
        cache_key="k|serving:SG", req=_Req(), query="sunscreen", market="US", query_semantic_class=None, limit=10,
        page_offset=0, build_budget_ms=100, build_concurrency=1, include_seed_data_text_match=False,
        enable_broad_fallback=False, expansion_terms=None, brand_terms=None, brand_required_terms=None,
        brand_prefer_terms=None, brand_query_detected=False, request_market=None, serving_market="SG",
    )
    assert scheduled is True
    task = agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.get("k|serving:SG")
    assert task is not None
    await asyncio.wait_for(task, timeout=5)
    assert seen and seen[-1]["serving_market"] == "SG" and seen[-1]["market"] == "US" and seen[-1]["request_market"] is None
