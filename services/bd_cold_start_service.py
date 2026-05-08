"""
BD cold-start audit orchestrator.

Combines:
  1. Brand-name extraction from the target's homepage
     (services.brand_product_discovery._extract_brand_name)
  2. Primary discovery via Pivota-catalog-intelligence service
     (services.catalog_intelligence_client) — Puppeteer-backed,
     handles JS-rendered storefronts, returns rich catalog data
  3. Fallback discovery via in-process sitemap + link crawl
     (services.brand_product_discovery) — when catalog-intelligence
     is unreachable / unconfigured / returns empty
  4. Backfill to prospect_products table — every cold audit grows
     Pivota's catalog of D2C brand data
  5. Returns the audit-shaped products list (handed to
     run_brand_report by the route handler)

Why orchestrator pattern: the caller doesn't need to know which
discovery strategy worked. It gets a uniform shape. Diagnostics on
the response indicate which path fired so operators can debug
without reading logs.

Honesty rules:
  - Never fabricates a brand name or product title.
  - When all discovery paths fail, raises BrandDiscoveryError with
    operator-facing diagnostic.
  - Backfill is best-effort — DB failure doesn't fail the audit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class BrandDiscoveryError(RuntimeError):
    """Raised when both catalog-intelligence and the in-process
    fallback fail to discover any products from the target URL.
    Caller should map to a 422 with diagnostic message + manual-
    entry fallback instruction."""


def _coverage_stats(raw_products: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute SKU / image / description coverage from
    catalog-intelligence's ExtractedProduct payloads. Mirrors the
    catalog-extract-audit codex skill's "summarize SKU coverage,
    image coverage, locale anomalies" output so BD operators can
    judge backfill safety from the response without reading
    raw_extracted JSON.
    """
    if not raw_products:
        return {
            "products_total": 0,
            "with_images": 0,
            "with_variants": 0,
            "with_descriptions": 0,
            "image_coverage_pct": None,
            "variant_coverage_pct": None,
        }
    total = len(raw_products)
    with_images = 0
    with_variants = 0
    with_descriptions = 0
    for p in raw_products:
        raw = p.get("raw_extracted") or {}
        if raw.get("image_url") or (raw.get("image_urls") or []):
            with_images += 1
        if raw.get("variant_skus") or raw.get("variants"):
            with_variants += 1
        if raw.get("description_raw"):
            with_descriptions += 1
    return {
        "products_total": total,
        "with_images": with_images,
        "with_variants": with_variants,
        "with_descriptions": with_descriptions,
        "image_coverage_pct": int(round(with_images / total * 100)),
        "variant_coverage_pct": int(round(with_variants / total * 100)),
    }


async def discover_products_for_audit(
    url: str,
    *,
    max_products: int = 3,
    market: str = "US",
    audit_run_id: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Orchestrate cold-start product discovery for BD audits.

    Returns:
      {
        "merchant_name": str,                    # extracted brand name
        "merchant_domain": str,                  # canonical domain
        "products": [                            # audit-ready shape
          {"title": str, "pdp_url": str,
           "vendor": None, "product_type": None},
          ...
        ],
        "discovery_method": str,                 # 'catalog_intelligence' |
                                                  # 'shopify_sitemap' |
                                                  # 'generic_sitemap' |
                                                  # 'homepage_links'
        "platform": str | None,                  # set by catalog-intelligence
                                                  # ('shopify' / etc.)
        "fallback_used": bool,                   # True if primary path
                                                  # was catalog-intelligence
                                                  # and it failed/empty
        "products_persisted": int,               # rows upserted to
                                                  # prospect_products
      }

    Raises BrandDiscoveryError when no products can be discovered.
    """
    from services.brand_product_discovery import (
        BrandProductDiscoveryError,
        _extract_brand_name,
        _fetch_text,
        _MAX_BYTES_HTML,
        _robots_allows,
        discover_products_from_homepage,
    )
    from services.catalog_intelligence_client import (
        extract_catalog,
        is_configured as catalog_intelligence_is_configured,
    )
    from db.prospect_products import upsert_discovered_products

    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BrandDiscoveryError(
            f"Invalid URL: {url!r}. Must start with http:// or https://"
        )
    base = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc

    # Robots.txt check before anything (mirrors the safety pattern
    # established in Phase B). Permissive on errors — only positive
    # Disallow blocks.
    if not await _robots_allows(base):
        raise BrandDiscoveryError(
            f"Site's robots.txt disallows our crawler "
            f"({base}/robots.txt). Cannot auto-discover products. "
            f"Fall back to manual product entry."
        )

    # Step 1: extract brand name from homepage. Both paths need it
    # (catalog-intelligence's API requires `brand`).
    homepage_html, homepage_status = await _fetch_text(
        base + "/", _MAX_BYTES_HTML,
    )
    if homepage_status != "ok":
        raise BrandDiscoveryError(
            f"Couldn't fetch homepage at {base}/ — {homepage_status}. "
            f"Site may be down or blocking our User-Agent."
        )
    merchant_name = _extract_brand_name(homepage_html, domain)

    # Step 2: try catalog-intelligence (primary). Best effort —
    # returns None on any failure so we fall back without raising.
    catalog_intelligence_used = False
    discovery_method = "unknown"
    platform: Optional[str] = None
    products: List[Dict[str, Any]] = []
    raw_payload_for_backfill: Optional[Dict[str, Any]] = None
    diagnostics: Optional[Dict[str, Any]] = None

    if catalog_intelligence_is_configured():
        catalog_intelligence_used = True
        ci_result = await extract_catalog(
            brand=merchant_name,
            domain=domain,
            market=market,
            limit=max(max_products * 4, 20),  # over-fetch so backfill
                                                # captures more than
                                                # the audited subset
        )
        if ci_result and ci_result.get("products"):
            products = ci_result["products"]
            platform = ci_result.get("platform")
            discovery_method = "catalog_intelligence"
            raw_payload_for_backfill = ci_result
            diagnostics = ci_result.get("diagnostics")
            # Mirror the catalog-extract-audit skill: log diagnostics
            # structurally so ops can spot patterns (which sites get
            # bot-challenged consistently? which trigger
            # block_provider=cloudflare?). Surface to caller as well.
            failure_category = (diagnostics or {}).get("failure_category")
            if failure_category:
                logger.warning(
                    "bd_cold_start: catalog-intelligence reported "
                    "failure_category=%s for domain=%s "
                    "(strategy=%s block_provider=%s) — products "
                    "still returned, backfill proceeding but BD "
                    "operator should review diagnostics before "
                    "trusting verdict",
                    failure_category, domain,
                    (diagnostics or {}).get("discovery_strategy"),
                    (diagnostics or {}).get("block_provider"),
                )

    # Step 3: fall back to in-process discovery if catalog-intelligence
    # didn't yield products. Either it's not configured, was
    # unreachable, returned empty, or didn't recognize the storefront.
    fallback_used = False
    if not products:
        fallback_used = catalog_intelligence_used
        try:
            fallback_result = await discover_products_from_homepage(
                base + "/", max_products=max(max_products * 2, 6),
            )
        except BrandProductDiscoveryError as exc:
            # Both paths failed. Raise with the lightweight-discovery
            # diagnostic — that error message is operator-facing.
            raise BrandDiscoveryError(
                f"Catalog-intelligence "
                f"{'failed; ' if catalog_intelligence_used else 'not configured. '}"
                f"Fallback discovery also failed: {exc}"
            )
        # Adopt the fallback's brand name only when we couldn't
        # extract one ourselves (defensive — _extract_brand_name
        # already ran above and should match).
        if not merchant_name or merchant_name == domain:
            merchant_name = fallback_result["merchant_name"] or merchant_name
        products = fallback_result["products"]
        discovery_method = fallback_result["discovery_method"]

    # Trim to requested audit size. Backfill captures the FULL
    # discovered list (not just the audited subset) — that's the
    # whole point of using catalog-intelligence's wider crawl.
    audit_products = products[:max_products]

    # Step 4: backfill to prospect_products. Best-effort. Skipped
    # entirely when persist=False (dry-run mode — see catalog-extract-
    # audit codex skill's "preflight before any seed writes" pattern).
    products_persisted = 0
    if persist:
        try:
            products_persisted = await upsert_discovered_products(
                prospect_brand=merchant_name,
                prospect_domain=domain,
                discovery_source=discovery_method,
                products=products,
                audit_run_id=audit_run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bd_cold_start: prospect_products backfill failed for %s: %s",
                domain, exc,
            )

    return {
        "merchant_name": merchant_name,
        "merchant_domain": domain,
        "products": audit_products,
        "discovery_method": discovery_method,
        "platform": platform,
        "fallback_used": fallback_used,
        "products_discovered_total": len(products),
        "products_persisted": products_persisted,
        # Catalog-intelligence diagnostics passed through so BD
        # operators can apply the catalog-extract-audit skill's
        # "is this safe to backfill" judgment. None when fallback
        # discovery was used (no upstream diagnostics to forward).
        "diagnostics": diagnostics,
        # SKU / image / description coverage stats — same shape the
        # catalog-extract-audit skill emits. None when fallback path
        # used (raw_extracted not populated for fallback products).
        "coverage": _coverage_stats(products) if discovery_method == "catalog_intelligence" else None,
    }
