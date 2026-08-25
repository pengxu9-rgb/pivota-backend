"""
BD cold-start audit — auto-discover products from a brand's website.

Use case: BD does cold outreach to a new brand. They have the URL
(maybe the brand name) and nothing else — no product list, no PDPs.
Filling the existing BD audit form (merchant_name + 1-5 product
title/url/vendor/type rows) is 5-10 min of manual work per target.

This module reduces that to: paste a URL, get a working audit.

Discovery strategies, in order of accuracy:
  1. Shopify sitemap (`sitemap_products_*.xml`) — covers ~80% of D2C
     cold-outreach targets. Provides direct product URLs + lastmod
     timestamps; we pick the most-recently-updated N.
  2. Generic sitemap.xml — non-Shopify CMSes (BigCommerce, Squarespace
     Commerce, custom). Filtered to URLs that look like products
     (path segments matching common patterns).
  3. Homepage link crawling — last-resort scan of <a> hrefs for
     product-shaped paths. Fragile; sites that JS-render their nav
     produce empty results.

Honesty rules:
  - When discovery fails (no sitemap, no product-shaped links,
    robots-blocked), the function raises BrandProductDiscoveryError
    with a SPECIFIC reason — never returns fabricated products.
  - Brand name extracted from <title>/<meta og:site_name>/domain;
    if extraction is ambiguous, fall back to domain. Don't invent.
  - Product titles extracted from <title>, <h1>, or og:title — never
    synthesized from the URL slug (slugs are dehumanized).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET
from services import crawl_politeness

logger = logging.getLogger(__name__)


_USER_AGENT = (
    "PivotaAuditBot/1.0 (+https://pivota.cc/about/audit-bot; "
    "auto-discovers products for BD cold-start audits)"
)
_FETCH_TIMEOUT_SECONDS = 8.0
_MAX_BYTES_HTML = 1 * 1024 * 1024     # 1MB caps the homepage fetch
_MAX_BYTES_XML = 5 * 1024 * 1024      # 5MB caps a sitemap (Shopify
                                       # sitemap_products_1.xml is
                                       # typically <500KB even for
                                       # large catalogs)


class BrandProductDiscoveryError(RuntimeError):
    """Raised when we can't auto-discover products from a URL.
    Caller (BD route handler) should surface as a 422 with the
    reason so the BD operator knows whether to retry, fall back to
    manual entry, or skip the brand entirely."""


async def discover_products_from_homepage(
    url: str,
    *,
    max_products: int = 3,
) -> Dict[str, Any]:
    """Discover up to `max_products` PDPs from a brand's homepage.

    Returns:
      {
        "merchant_name": str,        # extracted brand name
        "merchant_domain": str,      # canonical domain
        "products": [
          {"title": str, "pdp_url": str, "vendor": None,
           "product_type": None},
          ...
        ],
        "discovery_method": "shopify_sitemap" | "generic_sitemap"
                          | "homepage_links",
      }

    Raises BrandProductDiscoveryError when no products can be
    discovered. The error message is operator-facing.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BrandProductDiscoveryError(
            f"Invalid URL: {url!r}. Must start with http:// or https://"
        )
    base = f"{parsed.scheme}://{parsed.netloc}"

    # The FULL url, not `base` — see the note in the helper. A bare origin normalizes to
    # "/", so passing it re-asks about the site root and the delegation buys nothing.
    if not await _robots_allows(url):
        raise BrandProductDiscoveryError(
            f"Site's robots.txt disallows our crawler "
            f"({base}/robots.txt). Cannot auto-discover products. "
            f"Fall back to manual product entry."
        )

    homepage_html, homepage_status = await _fetch_text(base + "/", _MAX_BYTES_HTML)
    if homepage_status != "ok":
        raise BrandProductDiscoveryError(
            f"Couldn't fetch homepage at {base}/ — {homepage_status}. "
            f"Site may be down or blocking our User-Agent."
        )
    merchant_name = _extract_brand_name(homepage_html, parsed.netloc)
    merchant_domain = parsed.netloc

    # Strategy 1: Shopify sitemap_products_*.xml
    products = await _discover_via_shopify_sitemap(base, max_products)
    if products:
        return _build_result(
            merchant_name, merchant_domain, products,
            "shopify_sitemap",
        )

    # Strategy 2: Generic sitemap.xml filtered to product-shaped URLs
    products = await _discover_via_generic_sitemap(base, max_products)
    if products:
        return _build_result(
            merchant_name, merchant_domain, products,
            "generic_sitemap",
        )

    # Strategy 3: Homepage link crawl
    products = await _discover_via_homepage_links(
        base, homepage_html, max_products,
    )
    if products:
        return _build_result(
            merchant_name, merchant_domain, products,
            "homepage_links",
        )

    raise BrandProductDiscoveryError(
        f"Couldn't auto-discover any products at {base}. The site "
        f"may not have a sitemap, may not use a recognized e-commerce "
        f"platform, or may be JavaScript-rendered. Fall back to manual "
        f"product entry."
    )


def _build_result(
    merchant_name: str,
    merchant_domain: str,
    products: List[Dict[str, Any]],
    method: str,
) -> Dict[str, Any]:
    return {
        "merchant_name": merchant_name,
        "merchant_domain": merchant_domain,
        "products": products,
        "discovery_method": method,
    }


# ----------------------------------------------------------------------
# Strategy 1 — Shopify sitemap_products_*.xml
# ----------------------------------------------------------------------


async def _discover_via_shopify_sitemap(
    base: str,
    max_products: int,
) -> List[Dict[str, Any]]:
    """Shopify exposes /sitemap.xml as an index that points to
    sitemap_products_1.xml + sitemap_collections_*.xml + others.
    The products sitemap has every product PDP with lastmod. We
    pick the N most-recently-updated.
    """
    index_xml, status = await _fetch_text(
        f"{base}/sitemap.xml", _MAX_BYTES_XML,
    )
    if status != "ok":
        return []
    product_sitemap_urls = _extract_shopify_product_sitemap_urls(index_xml)
    if not product_sitemap_urls:
        return []
    product_urls: List[Tuple[str, Optional[str]]] = []  # (url, lastmod)
    for sm_url in product_sitemap_urls[:3]:  # cap fan-out; 3 sitemaps cover
                                              # 50k products at Shopify limit
        sm_xml, sm_status = await _fetch_text(sm_url, _MAX_BYTES_XML)
        if sm_status != "ok":
            continue
        product_urls.extend(_extract_urls_with_lastmod(sm_xml))
    if not product_urls:
        return []
    # Most-recently-updated first.
    product_urls.sort(key=lambda x: x[1] or "", reverse=True)
    selected_urls = [u for u, _ in product_urls[: max_products * 2]]
    # Hydrate titles from each PDP. Take first max_products that
    # successfully fetch.
    return await _hydrate_products(selected_urls, max_products)


def _extract_shopify_product_sitemap_urls(index_xml: str) -> List[str]:
    """Parse a sitemap-index XML, return the URLs of any
    `sitemap_products_*.xml` entries."""
    try:
        root = ET.fromstring(index_xml)
    except ET.ParseError:
        return []
    out: List[str] = []
    # sitemap-index uses sitemapindex/sitemap/loc; namespace may vary.
    for loc in root.iter():
        tag = loc.tag.lower()
        if not tag.endswith("loc"):
            continue
        text = (loc.text or "").strip()
        if "sitemap_products" in text and text.endswith(".xml"):
            out.append(text)
    return out


def _extract_urls_with_lastmod(
    sitemap_xml: str,
) -> List[Tuple[str, Optional[str]]]:
    """Parse a regular sitemap, return (loc, lastmod) tuples."""
    try:
        root = ET.fromstring(sitemap_xml)
    except ET.ParseError:
        return []
    out: List[Tuple[str, Optional[str]]] = []
    for url_node in root.iter():
        if not url_node.tag.lower().endswith("url"):
            continue
        loc: Optional[str] = None
        lastmod: Optional[str] = None
        for child in url_node:
            child_tag = child.tag.lower()
            if child_tag.endswith("loc"):
                loc = (child.text or "").strip() or None
            elif child_tag.endswith("lastmod"):
                lastmod = (child.text or "").strip() or None
        if loc:
            out.append((loc, lastmod))
    return out


# ----------------------------------------------------------------------
# Strategy 2 — Generic sitemap.xml URL filter
# ----------------------------------------------------------------------


# URL path patterns that strongly suggest a product PDP. Conservative —
# false positives (e.g., a /product-info page) are worse than missing
# a product (we'd fall back to homepage crawl).
_PRODUCT_PATH_PATTERNS = (
    re.compile(r"/products?/[^/?#]+"),
    re.compile(r"/shop/[^/?#]+"),
    re.compile(r"/p/[^/?#]+"),
    re.compile(r"/item/[^/?#]+"),
)


async def _discover_via_generic_sitemap(
    base: str,
    max_products: int,
) -> List[Dict[str, Any]]:
    """Non-Shopify sites may have a sitemap.xml without the
    `sitemap_products_*` index pattern. Walk the sitemap (or
    sitemap-index), filter URLs whose paths look product-shaped."""
    sm_xml, status = await _fetch_text(
        f"{base}/sitemap.xml", _MAX_BYTES_XML,
    )
    if status != "ok":
        return []
    # If this is a sitemap-index, pull child sitemaps and filter.
    child_sitemaps = _extract_sitemap_index_children(sm_xml)
    candidate_urls: List[str] = []
    if child_sitemaps:
        for child in child_sitemaps[:3]:
            child_xml, child_status = await _fetch_text(child, _MAX_BYTES_XML)
            if child_status == "ok":
                for loc, _ in _extract_urls_with_lastmod(child_xml):
                    if _looks_like_product_url(loc):
                        candidate_urls.append(loc)
                if len(candidate_urls) >= max_products * 2:
                    break
    else:
        for loc, _ in _extract_urls_with_lastmod(sm_xml):
            if _looks_like_product_url(loc):
                candidate_urls.append(loc)
            if len(candidate_urls) >= max_products * 2:
                break
    if not candidate_urls:
        return []
    return await _hydrate_products(candidate_urls[: max_products * 2], max_products)


def _extract_sitemap_index_children(xml: str) -> List[str]:
    """Returns child sitemap URLs from a sitemap-index doc.
    Empty list if this isn't an index."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    if not root.tag.lower().endswith("sitemapindex"):
        return []
    out: List[str] = []
    for loc in root.iter():
        if loc.tag.lower().endswith("loc"):
            text = (loc.text or "").strip()
            if text:
                out.append(text)
    return out


def _looks_like_product_url(url: str) -> bool:
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return False
    return any(p.search(path) for p in _PRODUCT_PATH_PATTERNS)


# ----------------------------------------------------------------------
# Strategy 3 — Homepage link crawl (last-resort)
# ----------------------------------------------------------------------


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


async def _discover_via_homepage_links(
    base: str,
    homepage_html: str,
    max_products: int,
) -> List[Dict[str, Any]]:
    """Last-resort: scan the homepage HTML for <a href="..."> links
    matching product-URL patterns. Fragile — sites that JS-render
    their nav produce empty results — but covers small custom sites."""
    candidate_paths: List[str] = []
    seen: set = set()
    for match in _HREF_RE.finditer(homepage_html):
        href = match.group(1).strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        # Resolve relative links against base.
        absolute = urljoin(base + "/", href)
        try:
            parsed = urlparse(absolute)
        except ValueError:
            continue
        # Same-origin only — don't follow external links.
        base_host = urlparse(base).netloc.lower()
        if (parsed.netloc or base_host).lower() != base_host:
            continue
        if not _looks_like_product_url(absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        candidate_paths.append(absolute)
        if len(candidate_paths) >= max_products * 2:
            break
    if not candidate_paths:
        return []
    return await _hydrate_products(candidate_paths, max_products)


# ----------------------------------------------------------------------
# Brand name + product title extraction
# ----------------------------------------------------------------------


_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE | re.DOTALL)
_OG_SITE_NAME_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_H1_RE = re.compile(r"<h1[^>]*>([^<]+)</h1>", re.IGNORECASE | re.DOTALL)


def _extract_brand_name(html: str, fallback_domain: str) -> str:
    """Extract a human brand name from the homepage. og:site_name is
    the cleanest source; <title> is a fallback (often "Brand Name —
    Tagline"); domain is last-resort.

    Honesty: never INVENT a brand name. If extraction is ambiguous,
    return the domain — better to surface a less-pretty name than a
    wrong one.
    """
    m = _OG_SITE_NAME_RE.search(html)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    m = _TITLE_RE.search(html)
    if m:
        title = m.group(1).strip()
        # Strip common separator suffixes: "Brand — Tagline", "Brand | Tagline"
        for sep in (" — ", " | ", " - "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        if title:
            return title
    return fallback_domain


def _extract_product_title(html: str, url: str) -> Optional[str]:
    """og:title > <title> > <h1>. Returns None when nothing extractable
    — the caller skips the URL rather than synthesizing a title from
    the slug."""
    m = _OG_TITLE_RE.search(html)
    if m:
        t = m.group(1).strip()
        if t:
            return t
    m = _TITLE_RE.search(html)
    if m:
        t = m.group(1).strip()
        # Strip "Product Name — Brand" suffixes.
        for sep in (" — ", " | ", " - "):
            if sep in t:
                t = t.split(sep)[0].strip()
                break
        if t:
            return t
    m = _H1_RE.search(html)
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()
        if t:
            return t
    return None


async def _hydrate_products(
    urls: List[str],
    max_products: int,
) -> List[Dict[str, Any]]:
    """Fetch each URL, extract a title, return up to max_products
    successfully-hydrated entries. Skips URLs that timeout, return
    non-200, or yield no extractable title — never fabricates."""
    out: List[Dict[str, Any]] = []
    for url in urls:
        if len(out) >= max_products:
            break
        html, status = await _fetch_text(url, _MAX_BYTES_HTML)
        if status != "ok":
            continue
        title = _extract_product_title(html, url)
        if not title:
            continue
        out.append({
            "title": title,
            "pdp_url": url,
            "vendor": None,
            "product_type": None,
        })
    return out


# ----------------------------------------------------------------------
# HTTP + robots.txt
# ----------------------------------------------------------------------


async def _fetch_text(url: str, max_bytes: int) -> Tuple[str, str]:
    """Returns (text, status). status one of: 'ok' | 'blocked' |
    'error'. Bounded by max_bytes to protect memory."""
    try:
        import httpx
    except ImportError:
        return ("", "error")
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            # Shared politeness gate. These lanes crawl merchant storefronts from the SAME
            # reserved NAT address as every other outbound lane, so a ban earned here takes the
            # whole crawl subnet down. `max_wait=0` because this is batch work: refusing rather
            # than waiting would turn a brief 429 into silently-dropped rows.
            # Bounded (default): reachable from POST /api/agent-center/bd/cold-start-audit.
            await crawl_politeness.before_request(url, user_agent=_USER_AGENT)
            resp = await client.get(url)
            crawl_politeness.note_response(
                url, resp.status_code, retry_after=resp.headers.get("retry-after")
            )
            if resp.status_code != 200:
                return ("", "error")
            content = resp.content[:max_bytes]
            try:
                return (
                    content.decode(resp.encoding or "utf-8", errors="ignore"),
                    "ok",
                )
            except Exception:
                return (content.decode("utf-8", errors="ignore"), "ok")
    except Exception as exc:  # noqa: BLE001
        logger.info("brand discovery fetch failed for %s: %s", url, exc)
        return ("", "error")


async def _robots_allows(url: str) -> bool:
    """Is `url` crawlable for our User-Agent? Delegates to the shared politeness gate.

    THIS USED TO ASK ABOUT THE SITE ROOT. The old body ended in
    `rp.can_fetch(_USER_AGENT, base + "/")`, so a `Disallow: /products/` never bit — and
    /products/ is exactly what this lane fetches. It read as a robots check and enforced almost
    nothing. `crawl_politeness.robots_allows` consults the FULL path, and additionally caches
    per host (this refetched robots.txt on every call), caps the response size, and bounds the
    redirect chain.

    Same permissive-on-error contract as before: only an explicit Disallow blocks.
    """
    return await crawl_politeness.robots_allows(url, user_agent=_USER_AGENT)
