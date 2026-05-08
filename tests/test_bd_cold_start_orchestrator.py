"""
BD cold-start orchestrator tests.

Verifies the two-path discovery + backfill flow:
  - catalog-intelligence is the primary path when configured
  - brand_product_discovery is the fallback when catalog-intelligence
    is unconfigured / unreachable / returns empty
  - Discovered products are persisted to prospect_products
  - Errors raise BrandDiscoveryError with operator-facing diagnostics
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def with_catalog_intelligence(monkeypatch):
    """Configure catalog-intelligence URL so client thinks it's
    reachable. Tests pick whether the actual call succeeds via
    other patches."""
    from config import settings as settings_module
    monkeypatch.setattr(
        settings_module.settings, "catalog_intelligence_base_url",
        "https://catalog-intel.test/",
    )
    monkeypatch.setattr(
        settings_module.settings, "catalog_intelligence_api_key", "test-key",
    )


@pytest.fixture
def without_catalog_intelligence(monkeypatch):
    """Catalog-intelligence not configured → orchestrator skips
    primary path entirely."""
    from config import settings as settings_module
    monkeypatch.setattr(
        settings_module.settings, "catalog_intelligence_base_url", "",
    )


def _gruns_homepage_html() -> str:
    return """
    <html><head>
      <title>Gruns — Daily Greens</title>
      <meta property="og:site_name" content="Gruns" />
    </head><body>
      <a href="/products/strawberry">Strawberry</a>
      <a href="/products/mango">Mango</a>
    </body></html>
    """


# ----------------------------------------------------------------------
# catalog_intelligence_client
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_intelligence_returns_none_when_not_configured(
    without_catalog_intelligence,
):
    from services.catalog_intelligence_client import extract_catalog
    result = await extract_catalog(brand="Gruns", domain="gruns.co")
    assert result is None


@pytest.mark.asyncio
async def test_catalog_intelligence_normalizes_extracted_products(
    with_catalog_intelligence,
):
    """Successful response from catalog-intelligence: the rich
    ExtractedProduct shape gets projected to {title, pdp_url,
    vendor, product_type, raw_extracted}."""
    from services import catalog_intelligence_client as mod

    from unittest.mock import MagicMock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "brand": "Gruns",
        "domain": "gruns.co",
        "platform": "shopify",
        "products": [
            {
                "title": "Strawberry Gummies",
                "url": "https://gruns.co/products/strawberry",
                "image_url": "https://gruns.co/cdn/x.jpg",
                "variant_skus": ["sku-1", "sku-2"],
                "variants": [],
            },
            {
                "title": "Mango Gummies",
                "url": "https://gruns.co/products/mango",
                "image_url": "https://gruns.co/cdn/y.jpg",
                "variant_skus": [],
                "variants": [],
            },
        ],
        "diagnostics": {
            "discovery_strategy": "shopify_json",
        },
    })

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return mock_response

    with patch("httpx.AsyncClient", _Client):
        result = await mod.extract_catalog(brand="Gruns", domain="gruns.co")
    assert result is not None
    assert result["platform"] == "shopify"
    assert result["discovery_strategy"] == "shopify_json"
    assert len(result["products"]) == 2
    p = result["products"][0]
    assert p["title"] == "Strawberry Gummies"
    assert p["pdp_url"] == "https://gruns.co/products/strawberry"
    assert p["vendor"] is None
    assert p["product_type"] is None
    # Raw payload preserved for backfill
    assert p["raw_extracted"]["variant_skus"] == ["sku-1", "sku-2"]


def test_normalize_base_url_auto_prepends_scheme():
    """Footgun fix: env var set without `https://` doesn't silently
    break catalog-intelligence integration. Was burned by this on
    gruns.co smoke test."""
    from services.catalog_intelligence_client import _normalize_base_url
    assert _normalize_base_url(
        "pivota-catalog-intelligence-production.up.railway.app"
    ) == "https://pivota-catalog-intelligence-production.up.railway.app"


def test_normalize_base_url_preserves_existing_scheme():
    from services.catalog_intelligence_client import _normalize_base_url
    assert _normalize_base_url("https://example.com/") == "https://example.com"
    assert _normalize_base_url("http://localhost:3000") == "http://localhost:3000"


def test_normalize_base_url_handles_empty_or_whitespace():
    from services.catalog_intelligence_client import _normalize_base_url
    assert _normalize_base_url("") == ""
    assert _normalize_base_url("   ") == ""
    assert _normalize_base_url(None) == ""


def test_normalize_base_url_strips_trailing_slash():
    from services.catalog_intelligence_client import _normalize_base_url
    assert _normalize_base_url("example.com/") == "https://example.com"
    assert _normalize_base_url("https://example.com////") == "https://example.com"


@pytest.mark.asyncio
async def test_catalog_intelligence_handles_unsupported_protocol(
    with_catalog_intelligence, monkeypatch,
):
    """Defense-in-depth: if _normalize_base_url somehow lets through
    an invalid URL (e.g., env var has whitespace mid-string),
    UnsupportedProtocol is caught + logged at ERROR with the
    resolved URL so operators can fix the env var."""
    from config import settings as settings_module
    from services import catalog_intelligence_client as mod
    import httpx

    # Simulate a corrupted URL by setting one that bypasses
    # the normalizer's scheme check.
    monkeypatch.setattr(
        settings_module.settings,
        "catalog_intelligence_base_url",
        "ftp://wrong-scheme.example",
    )

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            raise httpx.UnsupportedProtocol("bad protocol")

    with patch("httpx.AsyncClient", _Client):
        result = await mod.extract_catalog(brand="X", domain="x.com")
    assert result is None  # falls back gracefully


@pytest.mark.asyncio
async def test_catalog_intelligence_returns_none_on_network_error(
    with_catalog_intelligence,
):
    from services import catalog_intelligence_client as mod
    import httpx

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            raise httpx.ConnectError("network unreachable")

    with patch("httpx.AsyncClient", _Client):
        result = await mod.extract_catalog(brand="X", domain="x.com")
    assert result is None  # never raises into orchestrator


@pytest.mark.asyncio
async def test_catalog_intelligence_returns_none_on_5xx(
    with_catalog_intelligence,
):
    from services import catalog_intelligence_client as mod
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.text = "service unavailable"

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return mock_response

    with patch("httpx.AsyncClient", _Client):
        result = await mod.extract_catalog(brand="X", domain="x.com")
    assert result is None


@pytest.mark.asyncio
async def test_catalog_intelligence_returns_none_on_empty_products(
    with_catalog_intelligence,
):
    from services import catalog_intelligence_client as mod
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "brand": "X", "domain": "x.com", "products": [],
    })

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return mock_response

    with patch("httpx.AsyncClient", _Client):
        result = await mod.extract_catalog(brand="X", domain="x.com")
    assert result is None


@pytest.mark.asyncio
async def test_catalog_intelligence_skips_products_missing_url_or_title(
    with_catalog_intelligence,
):
    """Honesty: products without url or title get skipped — never
    synthesized. If ALL products lack required fields, returns None."""
    from services import catalog_intelligence_client as mod
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={
        "products": [
            {"title": "", "url": "https://x.com/p/1"},          # no title
            {"title": "Foo", "url": ""},                          # no url
            {"title": "Bar", "url": "https://x.com/p/3"},         # ok
        ],
    })

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return mock_response

    with patch("httpx.AsyncClient", _Client):
        result = await mod.extract_catalog(brand="X", domain="x.com")
    assert result is not None
    assert len(result["products"]) == 1
    assert result["products"][0]["title"] == "Bar"


# ----------------------------------------------------------------------
# Orchestrator end-to-end
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_uses_catalog_intelligence_when_available(
    with_catalog_intelligence,
):
    """Primary path: catalog-intelligence returns rich data; fallback
    is NOT invoked. Backfill upsert hits prospect_products."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    ci_result = {
        "products": [
            {"title": "Strawberry", "pdp_url": "https://gruns.co/products/strawberry",
             "vendor": None, "product_type": None,
             "raw_extracted": {"variant_skus": ["sku-1"]}},
            {"title": "Mango", "pdp_url": "https://gruns.co/products/mango",
             "vendor": None, "product_type": None, "raw_extracted": {}},
            {"title": "Orange", "pdp_url": "https://gruns.co/products/orange",
             "vendor": None, "product_type": None, "raw_extracted": {}},
        ],
        "platform": "shopify",
        "discovery_strategy": "shopify_json",
    }
    upsert_calls = []
    async def fake_upsert(**kw):
        upsert_calls.append(kw)
        return len(kw.get("products") or [])

    async def fake_fallback(*a, **kw):
        # Should NOT be called when catalog-intelligence succeeds.
        raise AssertionError("fallback should not run")

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=ci_result)):
                with patch.object(bpd, "discover_products_from_homepage",
                                  AsyncMock(side_effect=fake_fallback)):
                    with patch.object(pp, "upsert_discovered_products",
                                      AsyncMock(side_effect=fake_upsert)):
                        result = await mod.discover_products_for_audit(
                            "https://gruns.co/", max_products=2,
                        )

    assert result["discovery_method"] == "catalog_intelligence"
    assert result["platform"] == "shopify"
    assert result["fallback_used"] is False
    # Audit subset capped at max_products=2
    assert len(result["products"]) == 2
    # Backfill captured ALL discovered (3), not just the audited 2
    assert result["products_discovered_total"] == 3
    assert upsert_calls[0]["prospect_brand"] == "Gruns"
    assert upsert_calls[0]["prospect_domain"] == "gruns.co"
    assert upsert_calls[0]["discovery_source"] == "catalog_intelligence"
    assert len(upsert_calls[0]["products"]) == 3


@pytest.mark.asyncio
async def test_orchestrator_falls_back_when_catalog_intelligence_returns_none(
    with_catalog_intelligence,
):
    """catalog-intelligence configured but returns None (e.g.,
    bot-blocked / non-Shopify). Orchestrator falls back to
    brand_product_discovery."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    fallback_result = {
        "merchant_name": "Gruns",
        "merchant_domain": "gruns.co",
        "products": [
            {"title": "Strawberry", "pdp_url": "https://gruns.co/products/strawberry",
             "vendor": None, "product_type": None},
        ],
        "discovery_method": "shopify_sitemap",
    }

    upsert_calls = []
    async def fake_upsert(**kw):
        upsert_calls.append(kw)
        return len(kw["products"])

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=None)):
                with patch.object(bpd, "discover_products_from_homepage",
                                  AsyncMock(return_value=fallback_result)):
                    with patch.object(pp, "upsert_discovered_products",
                                      AsyncMock(side_effect=fake_upsert)):
                        result = await mod.discover_products_for_audit(
                            "https://gruns.co/", max_products=3,
                        )

    assert result["discovery_method"] == "shopify_sitemap"  # from fallback
    assert result["fallback_used"] is True
    assert len(result["products"]) == 1
    assert upsert_calls[0]["discovery_source"] == "shopify_sitemap"


@pytest.mark.asyncio
async def test_orchestrator_skips_catalog_intelligence_when_unconfigured(
    without_catalog_intelligence,
):
    """No catalog-intelligence URL → orchestrator skips primary
    path entirely (no HTTP call attempted), goes straight to
    fallback. fallback_used=False because primary wasn't tried."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    fallback_result = {
        "merchant_name": "Gruns", "merchant_domain": "gruns.co",
        "products": [{"title": "X", "pdp_url": "https://gruns.co/products/x",
                      "vendor": None, "product_type": None}],
        "discovery_method": "shopify_sitemap",
    }

    ci_call_count = 0
    async def fake_ci_extract(**kw):
        nonlocal ci_call_count
        ci_call_count += 1
        return None

    async def fake_upsert(**kw): return 1

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(side_effect=fake_ci_extract)):
                with patch.object(bpd, "discover_products_from_homepage",
                                  AsyncMock(return_value=fallback_result)):
                    with patch.object(pp, "upsert_discovered_products",
                                      AsyncMock(side_effect=fake_upsert)):
                        result = await mod.discover_products_for_audit(
                            "https://gruns.co/",
                        )

    # When catalog-intelligence isn't configured, ci.extract_catalog
    # short-circuits to None without any HTTP attempt — but since
    # is_configured() returns False, the orchestrator skips the call
    # entirely. Either way, fallback_used=False (we never TRIED the
    # primary path, so it didn't "fail").
    assert result["discovery_method"] == "shopify_sitemap"
    assert result["fallback_used"] is False


@pytest.mark.asyncio
async def test_orchestrator_raises_when_both_paths_fail(
    with_catalog_intelligence,
):
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=None)):
                with patch.object(
                    bpd, "discover_products_from_homepage",
                    AsyncMock(side_effect=bpd.BrandProductDiscoveryError(
                        "no sitemap, no product links"
                    )),
                ):
                    with pytest.raises(mod.BrandDiscoveryError) as ei:
                        await mod.discover_products_for_audit(
                            "https://lockedout.example/",
                        )
    assert "fallback discovery also failed" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_orchestrator_raises_on_robots_block(with_catalog_intelligence):
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd

    with patch.object(bpd, "_robots_allows", AsyncMock(return_value=False)):
        with pytest.raises(mod.BrandDiscoveryError) as ei:
            await mod.discover_products_for_audit("https://x.com/")
    assert "robots.txt" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_orchestrator_raises_on_unreachable_homepage(
    with_catalog_intelligence,
):
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd

    async def fake_robots(_): return True
    async def fake_fetch_fail(_url, _max):
        return ("", "error")

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch_fail)):
            with pytest.raises(mod.BrandDiscoveryError) as ei:
                await mod.discover_products_for_audit("https://gone.example/")
    assert "homepage" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_orchestrator_rejects_non_http_url():
    from services.bd_cold_start_service import (
        BrandDiscoveryError,
        discover_products_for_audit,
    )
    with pytest.raises(BrandDiscoveryError):
        await discover_products_for_audit("ftp://x.com/")
    with pytest.raises(BrandDiscoveryError):
        await discover_products_for_audit("not a url")


@pytest.mark.asyncio
async def test_orchestrator_skips_backfill_in_dry_run(
    with_catalog_intelligence,
):
    """Dry-run mode (catalog-extract-audit skill pattern): discovery
    runs, diagnostics + coverage computed, but NO upsert to
    prospect_products."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    ci_result = {
        "products": [
            {"title": "Strawberry", "pdp_url": "https://gruns.co/products/strawberry",
             "vendor": None, "product_type": None,
             "raw_extracted": {
                 "image_url": "https://gruns.co/cdn/x.jpg",
                 "variant_skus": ["sku-1"],
                 "description_raw": "Daily greens for kids",
             }},
        ],
        "platform": "shopify",
        "diagnostics": {
            "discovery_strategy": "shopify_json",
            "failure_category": None,
            "block_provider": None,
        },
    }

    upsert_count = 0
    async def fake_upsert(**kw):
        nonlocal upsert_count
        upsert_count += 1
        return 1

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=ci_result)):
                with patch.object(pp, "upsert_discovered_products",
                                  AsyncMock(side_effect=fake_upsert)):
                    result = await mod.discover_products_for_audit(
                        "https://gruns.co/", persist=False,
                    )

    assert result["discovery_method"] == "catalog_intelligence"
    assert result["products_persisted"] == 0
    assert upsert_count == 0  # No DB writes in dry-run


@pytest.mark.asyncio
async def test_orchestrator_surfaces_diagnostics_from_catalog_intelligence(
    with_catalog_intelligence,
):
    """catalog-extract-audit skill output: diagnostics from
    catalog-intelligence flow through to the orchestrator response
    so BD operators can judge backfill safety."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    ci_result = {
        "products": [{"title": "X", "pdp_url": "https://gruns.co/products/x",
                      "vendor": None, "product_type": None,
                      "raw_extracted": {"image_url": "https://x", "variant_skus": ["s"]}}],
        "platform": "shopify",
        "diagnostics": {
            "discovery_strategy": "shopify_json",
            "failure_category": "bot_challenge",
            "block_provider": "cloudflare",
            "http_trace": [{"url": "https://gruns.co/", "status": 403}],
        },
    }

    async def fake_upsert(**kw): return 1

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=ci_result)):
                with patch.object(pp, "upsert_discovered_products",
                                  AsyncMock(side_effect=fake_upsert)):
                    result = await mod.discover_products_for_audit(
                        "https://gruns.co/",
                    )

    diagnostics = result["diagnostics"]
    assert diagnostics["failure_category"] == "bot_challenge"
    assert diagnostics["block_provider"] == "cloudflare"
    assert diagnostics["http_trace"][0]["status"] == 403
    # Coverage stats present too
    coverage = result["coverage"]
    assert coverage is not None
    assert coverage["products_total"] == 1
    assert coverage["with_images"] == 1
    assert coverage["with_variants"] == 1
    assert coverage["image_coverage_pct"] == 100


@pytest.mark.asyncio
async def test_orchestrator_coverage_is_none_for_fallback_path(
    with_catalog_intelligence,
):
    """Fallback discovery doesn't have raw_extracted (just title +
    URL from sitemap/links). Coverage stats are None to avoid
    false-positive "all products are missing images!" warnings —
    the fallback simply never had that data to begin with."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    fallback_result = {
        "merchant_name": "Gruns", "merchant_domain": "gruns.co",
        "products": [{"title": "X", "pdp_url": "https://gruns.co/p/x",
                      "vendor": None, "product_type": None}],
        "discovery_method": "shopify_sitemap",
    }

    async def fake_upsert(**kw): return 1

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=None)):
                with patch.object(bpd, "discover_products_from_homepage",
                                  AsyncMock(return_value=fallback_result)):
                    with patch.object(pp, "upsert_discovered_products",
                                      AsyncMock(side_effect=fake_upsert)):
                        result = await mod.discover_products_for_audit(
                            "https://gruns.co/",
                        )

    assert result["coverage"] is None
    assert result["diagnostics"] is None


def test_coverage_stats_computes_correctly():
    """Direct unit test on the helper — boundary behavior."""
    from services.bd_cold_start_service import _coverage_stats
    products = [
        {"raw_extracted": {"image_url": "x.jpg", "variant_skus": ["s1"], "description_raw": "x"}},
        {"raw_extracted": {"image_url": "", "variants": [{"sku": "s2"}], "description_raw": None}},
        {"raw_extracted": {"image_urls": ["a.jpg"]}},
        {"raw_extracted": {}},  # no fields
    ]
    stats = _coverage_stats(products)
    assert stats["products_total"] == 4
    assert stats["with_images"] == 2  # idx 0 (image_url), idx 2 (image_urls)
    assert stats["with_variants"] == 2  # idx 0 (variant_skus), idx 1 (variants)
    assert stats["with_descriptions"] == 1  # idx 0 only
    assert stats["image_coverage_pct"] == 50
    assert stats["variant_coverage_pct"] == 50


def test_coverage_stats_handles_empty_list():
    from services.bd_cold_start_service import _coverage_stats
    stats = _coverage_stats([])
    assert stats["products_total"] == 0
    assert stats["image_coverage_pct"] is None
    assert stats["variant_coverage_pct"] is None


@pytest.mark.asyncio
async def test_orchestrator_continues_when_backfill_db_fails(
    with_catalog_intelligence,
):
    """Backfill is best-effort. DB error doesn't fail the audit —
    discovered products still flow through to run_brand_report."""
    from services import bd_cold_start_service as mod
    from services import brand_product_discovery as bpd
    from services import catalog_intelligence_client as ci
    from db import prospect_products as pp

    async def fake_robots(_): return True
    async def fake_fetch(_url, _max):
        return (_gruns_homepage_html(), "ok")

    ci_result = {
        "products": [{"title": "X", "pdp_url": "https://gruns.co/products/x",
                      "vendor": None, "product_type": None,
                      "raw_extracted": {}}],
        "platform": "shopify",
    }

    async def boom(**kw):
        raise RuntimeError("db connection lost")

    with patch.object(bpd, "_robots_allows", AsyncMock(side_effect=fake_robots)):
        with patch.object(bpd, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
            with patch.object(ci, "extract_catalog", AsyncMock(return_value=ci_result)):
                with patch.object(pp, "upsert_discovered_products",
                                  AsyncMock(side_effect=boom)):
                    result = await mod.discover_products_for_audit(
                        "https://gruns.co/",
                    )
    # Audit data still flows through despite DB failure
    assert result["discovery_method"] == "catalog_intelligence"
    assert len(result["products"]) == 1
    assert result["products_persisted"] == 0  # DB failed
