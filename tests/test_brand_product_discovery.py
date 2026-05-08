"""
BD cold-start audit — product discovery tests.

Verifies:
  - Shopify sitemap path discovers products (covers ~80% of D2C)
  - Generic sitemap path filters URLs by product-shape
  - Homepage link crawl is the last-resort fallback
  - Discovery NEVER fabricates: if no titles extractable, the URL
    is skipped entirely (not synthesized from the slug)
  - Robots.txt is honored
  - Brand name extraction prefers og:site_name > <title> > domain
"""

from __future__ import annotations

from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------
# Brand-name + product-title extraction
# ---------------------------------------------------------------------


def test_brand_name_prefers_og_site_name():
    from services.brand_product_discovery import _extract_brand_name
    html = """
    <html><head>
      <title>Some pretty marketing title — Tagline</title>
      <meta property="og:site_name" content="Gruns" />
    </head></html>
    """
    assert _extract_brand_name(html, "gruns.co") == "Gruns"


def test_brand_name_falls_back_to_title_with_separator_strip():
    from services.brand_product_discovery import _extract_brand_name
    html = "<html><head><title>Lunya — Restful Sleepwear</title></head></html>"
    assert _extract_brand_name(html, "lunya.com") == "Lunya"


def test_brand_name_falls_back_to_domain():
    from services.brand_product_discovery import _extract_brand_name
    html = "<html><head></head><body>no metadata</body></html>"
    assert _extract_brand_name(html, "example.com") == "example.com"


def test_product_title_prefers_og_title():
    from services.brand_product_discovery import _extract_product_title
    html = """
    <html><head>
      <title>Slug-y title shouldn't win</title>
      <meta property="og:title" content="Strawberry Multivitamin Gummies" />
    </head></html>
    """
    assert _extract_product_title(html, "https://gruns.co/products/x") == \
        "Strawberry Multivitamin Gummies"


def test_product_title_strips_brand_suffix_from_title():
    from services.brand_product_discovery import _extract_product_title
    html = "<html><head><title>Strawberry Multivitamin — Gruns</title></head></html>"
    assert _extract_product_title(html, "...") == "Strawberry Multivitamin"


def test_product_title_falls_back_to_h1():
    from services.brand_product_discovery import _extract_product_title
    html = "<html><body><h1>Mango Pouches</h1></body></html>"
    assert _extract_product_title(html, "...") == "Mango Pouches"


def test_product_title_returns_none_when_no_extractable_title():
    """Honesty: never synthesize from URL slug."""
    from services.brand_product_discovery import _extract_product_title
    html = "<html><body><div>No title elements anywhere</div></body></html>"
    assert _extract_product_title(html, "https://x.com/products/cool-thing") is None


# ---------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------


def test_extract_shopify_product_sitemap_urls():
    from services.brand_product_discovery import _extract_shopify_product_sitemap_urls
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://gruns.co/sitemap_products_1.xml</loc></sitemap>
      <sitemap><loc>https://gruns.co/sitemap_collections_1.xml</loc></sitemap>
      <sitemap><loc>https://gruns.co/sitemap_products_2.xml</loc></sitemap>
      <sitemap><loc>https://gruns.co/sitemap_pages_1.xml</loc></sitemap>
    </sitemapindex>
    """
    out = _extract_shopify_product_sitemap_urls(index_xml)
    assert len(out) == 2
    assert all("sitemap_products" in u for u in out)


def test_extract_urls_with_lastmod():
    from services.brand_product_discovery import _extract_urls_with_lastmod
    sm = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://gruns.co/products/strawberry</loc><lastmod>2026-04-01</lastmod></url>
      <url><loc>https://gruns.co/products/mango</loc><lastmod>2026-05-01</lastmod></url>
      <url><loc>https://gruns.co/products/orange</loc></url>
    </urlset>
    """
    out = _extract_urls_with_lastmod(sm)
    assert len(out) == 3
    assert out[0] == ("https://gruns.co/products/strawberry", "2026-04-01")
    assert out[1] == ("https://gruns.co/products/mango", "2026-05-01")
    assert out[2] == ("https://gruns.co/products/orange", None)


def test_extract_handles_malformed_xml():
    from services.brand_product_discovery import (
        _extract_shopify_product_sitemap_urls,
        _extract_urls_with_lastmod,
    )
    assert _extract_shopify_product_sitemap_urls("not xml") == []
    assert _extract_urls_with_lastmod("<not>>xml") == []


# ---------------------------------------------------------------------
# URL pattern filter
# ---------------------------------------------------------------------


def test_looks_like_product_url_matches_common_paths():
    from services.brand_product_discovery import _looks_like_product_url
    assert _looks_like_product_url("https://x.com/products/strawberry-gummies")
    assert _looks_like_product_url("https://x.com/product/abc-123")
    assert _looks_like_product_url("https://x.com/shop/widget")
    assert _looks_like_product_url("https://x.com/p/sku123")
    assert _looks_like_product_url("https://x.com/item/foo-bar")


def test_looks_like_product_url_rejects_non_product_paths():
    from services.brand_product_discovery import _looks_like_product_url
    assert not _looks_like_product_url("https://x.com/")
    assert not _looks_like_product_url("https://x.com/about")
    assert not _looks_like_product_url("https://x.com/blog/post-1")
    assert not _looks_like_product_url("https://x.com/products/")  # trailing slash, no slug
    assert not _looks_like_product_url("https://x.com/products")   # no trailing slash, no slug


# ---------------------------------------------------------------------
# Discovery end-to-end (mocked HTTP)
# ---------------------------------------------------------------------


def _gruns_homepage_html() -> str:
    return """
    <html><head>
      <title>Gruns — Daily Greens for Kids</title>
      <meta property="og:site_name" content="Gruns" />
    </head><body>
      <a href="/products/strawberry">Strawberry</a>
      <a href="/products/mango">Mango</a>
    </body></html>
    """


def _gruns_sitemap_index() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://gruns.co/sitemap_products_1.xml</loc></sitemap>
    </sitemapindex>
    """


def _gruns_products_sitemap() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://gruns.co/products/strawberry</loc><lastmod>2026-05-01</lastmod></url>
      <url><loc>https://gruns.co/products/mango</loc><lastmod>2026-04-15</lastmod></url>
      <url><loc>https://gruns.co/products/orange</loc><lastmod>2026-03-01</lastmod></url>
    </urlset>
    """


def _product_pdp_html(name: str) -> str:
    return f"""
    <html><head>
      <title>{name} — Gruns</title>
      <meta property="og:title" content="{name}" />
    </head></html>
    """


@pytest.mark.asyncio
async def test_discovery_uses_shopify_sitemap_when_available():
    from services import brand_product_discovery as mod

    fetched_urls = []

    async def fake_fetch(url, _max_bytes):
        fetched_urls.append(url)
        if url.endswith("/robots.txt"):
            return ("", "error")  # No robots.txt → permissive
        if url == "https://gruns.co/":
            return (_gruns_homepage_html(), "ok")
        if url == "https://gruns.co/sitemap.xml":
            return (_gruns_sitemap_index(), "ok")
        if url == "https://gruns.co/sitemap_products_1.xml":
            return (_gruns_products_sitemap(), "ok")
        if url == "https://gruns.co/products/strawberry":
            return (_product_pdp_html("Strawberry Gummies"), "ok")
        if url == "https://gruns.co/products/mango":
            return (_product_pdp_html("Mango Gummies"), "ok")
        if url == "https://gruns.co/products/orange":
            return (_product_pdp_html("Orange Gummies"), "ok")
        return ("", "error")

    async def fake_robots(_base):
        return True

    with patch.object(mod, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
        with patch.object(mod, "_robots_allows", AsyncMock(side_effect=fake_robots)):
            result = await mod.discover_products_from_homepage(
                "https://gruns.co/", max_products=2,
            )

    assert result["merchant_name"] == "Gruns"
    assert result["merchant_domain"] == "gruns.co"
    assert result["discovery_method"] == "shopify_sitemap"
    assert len(result["products"]) == 2
    # Most-recently-updated first
    assert result["products"][0]["title"] == "Strawberry Gummies"
    assert result["products"][0]["pdp_url"] == "https://gruns.co/products/strawberry"
    assert result["products"][1]["title"] == "Mango Gummies"
    # vendor / product_type are intentionally None — Phase 1 doesn't infer
    assert all(p["vendor"] is None for p in result["products"])


@pytest.mark.asyncio
async def test_discovery_falls_back_to_homepage_links_when_sitemap_absent():
    from services import brand_product_discovery as mod

    async def fake_fetch(url, _max_bytes):
        if url == "https://example.co/":
            return (
                """<html><head><title>Example Brand</title></head>
                <body>
                  <a href="/products/foo">Foo</a>
                  <a href="/about">About</a>
                  <a href="/products/bar">Bar</a>
                </body></html>""",
                "ok",
            )
        if url == "https://example.co/sitemap.xml":
            return ("", "error")  # no sitemap
        if url == "https://example.co/products/foo":
            return (_product_pdp_html("Foo Widget"), "ok")
        if url == "https://example.co/products/bar":
            return (_product_pdp_html("Bar Gizmo"), "ok")
        return ("", "error")

    with patch.object(mod, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
        with patch.object(mod, "_robots_allows", AsyncMock(return_value=True)):
            result = await mod.discover_products_from_homepage(
                "https://example.co/", max_products=2,
            )

    assert result["discovery_method"] == "homepage_links"
    assert len(result["products"]) == 2
    titles = sorted(p["title"] for p in result["products"])
    assert titles == ["Bar Gizmo", "Foo Widget"]


@pytest.mark.asyncio
async def test_discovery_raises_when_robots_blocks():
    from services import brand_product_discovery as mod

    with patch.object(mod, "_robots_allows", AsyncMock(return_value=False)):
        with pytest.raises(mod.BrandProductDiscoveryError) as ei:
            await mod.discover_products_from_homepage(
                "https://blocked.example/", max_products=2,
            )
    assert "robots.txt" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_discovery_raises_when_homepage_unreachable():
    from services import brand_product_discovery as mod

    async def fake_fetch(_url, _max_bytes):
        return ("", "error")

    with patch.object(mod, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
        with patch.object(mod, "_robots_allows", AsyncMock(return_value=True)):
            with pytest.raises(mod.BrandProductDiscoveryError) as ei:
                await mod.discover_products_from_homepage(
                    "https://gone.example/", max_products=2,
                )
    assert "homepage" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_discovery_raises_when_no_strategy_yields_products():
    """Site exists, has homepage, but no sitemap, no product-shaped
    links anywhere. Discovery raises with operator-facing diagnostic."""
    from services import brand_product_discovery as mod

    async def fake_fetch(url, _max_bytes):
        if url.endswith("/"):
            # Homepage with NO product links
            return (
                "<html><head><title>Empty Site</title></head><body>nothing</body></html>",
                "ok",
            )
        return ("", "error")

    with patch.object(mod, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
        with patch.object(mod, "_robots_allows", AsyncMock(return_value=True)):
            with pytest.raises(mod.BrandProductDiscoveryError) as ei:
                await mod.discover_products_from_homepage(
                    "https://empty.example/", max_products=2,
                )
    assert "auto-discover" in str(ei.value).lower() or "fall back to manual" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_discovery_skips_urls_without_extractable_titles():
    """A discovered URL whose PDP HTML has no extractable title gets
    SKIPPED — never synthesized from the slug. Honesty rule."""
    from services import brand_product_discovery as mod

    async def fake_fetch(url, _max_bytes):
        if url.endswith("/"):
            return (_gruns_homepage_html(), "ok")
        if url == "https://gruns.co/sitemap.xml":
            return (_gruns_sitemap_index(), "ok")
        if url == "https://gruns.co/sitemap_products_1.xml":
            return (_gruns_products_sitemap(), "ok")
        if url == "https://gruns.co/products/strawberry":
            # No extractable title!
            return ("<html><body>nothing</body></html>", "ok")
        if url == "https://gruns.co/products/mango":
            return (_product_pdp_html("Mango Gummies"), "ok")
        if url == "https://gruns.co/products/orange":
            return (_product_pdp_html("Orange Gummies"), "ok")
        return ("", "error")

    with patch.object(mod, "_fetch_text", AsyncMock(side_effect=fake_fetch)):
        with patch.object(mod, "_robots_allows", AsyncMock(return_value=True)):
            result = await mod.discover_products_from_homepage(
                "https://gruns.co/", max_products=2,
            )

    # Strawberry got skipped (no title); 2 successfully hydrated
    titles = [p["title"] for p in result["products"]]
    assert "Mango Gummies" in titles
    assert "Orange Gummies" in titles


@pytest.mark.asyncio
async def test_discovery_rejects_non_http_urls():
    from services.brand_product_discovery import (
        BrandProductDiscoveryError,
        discover_products_from_homepage,
    )
    with pytest.raises(BrandProductDiscoveryError):
        await discover_products_from_homepage("ftp://gruns.co/", max_products=2)
    with pytest.raises(BrandProductDiscoveryError):
        await discover_products_from_homepage("not a url", max_products=2)
