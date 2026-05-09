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

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Shopify product page URL pattern: any URL whose path ends in
# /products/{handle}. Used to gate native-JSON refetch (Shopify always
# exposes products.json at the same path with .json appended). Other
# storefronts use different patterns; we skip the refetch for them.
_SHOPIFY_PRODUCT_URL_RE = re.compile(r"/products/[A-Za-z0-9_-]+/?(?:\?.*)?$")
_SHOPIFY_REFETCH_TIMEOUT_S = 6.0


class BrandDiscoveryError(RuntimeError):
    """Raised when both catalog-intelligence and the in-process
    fallback fail to discover any products from the target URL.
    Caller should map to a 422 with diagnostic message + manual-
    entry fallback instruction."""


async def _fetch_shopify_native(pdp_url: str) -> Optional[Dict[str, Any]]:
    """Fetch {pdp_url}.json from a Shopify storefront. Returns the
    `product` dict (vendor/product_type/tags/etc.) or None on any
    failure. Best-effort enrichment — the audit pipeline still works
    when this returns None.

    Shopify exposes a public JSON shape at every PDP URL when you
    append .json: GET https://store.com/products/foo.json →
    {product: {vendor: ..., product_type: ..., tags: ...}}. No auth
    needed. Cheap (~200-500ms typical).
    """
    if not pdp_url or not _SHOPIFY_PRODUCT_URL_RE.search(pdp_url):
        return None
    url = pdp_url.split("?", 1)[0].rstrip("/") + ".json"
    try:
        async with httpx.AsyncClient(
            timeout=_SHOPIFY_REFETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            r = await client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Pivota-BD-Audit/1.0",
                },
            )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.debug("shopify native refetch error for %s: %s", url, exc)
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    product = payload.get("product")
    return product if isinstance(product, dict) else None


async def _enrich_audit_products(
    products: List[Dict[str, Any]],
    *,
    merchant_name: str,
    domain: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fill in missing vendor + product_type on the audit-bound subset.

    Strategy stack (tried in order, per product):
      1. If source already has vendor/product_type (future-proof for
         when catalog-intelligence starts exposing them), keep them.
      2. Shopify-native .json refetch — works for any /products/{handle}
         URL; Shopify always populates `vendor`, sometimes populates
         `product_type`. Fast, parallel, no auth.
      3. Vendor fallback to discovered merchant_name (we always have
         this from the discovery step).
      4. product_type fallback via brand-level Gemini inference (single
         call applied to every product lacking a real product_type).
         See services/bd_brand_category_inferrer.py.

    Returns (enriched_products, diagnostics) where diagnostics records
    which strategies fired so BD operators can debug from the response
    without reading logs.
    """
    if not products:
        return products, {"enriched": False, "reason": "no_products"}

    # Step 1+2: parallel Shopify refetch for all products lacking
    # vendor or product_type. Source-provided values take precedence
    # over refetch (the refetch is only a fallback).
    refetch_targets = [
        i for i, p in enumerate(products)
        if not (p.get("vendor") and p.get("product_type"))
    ]
    refetch_count = 0
    if refetch_targets:
        urls = [products[i].get("pdp_url") or "" for i in refetch_targets]
        results = await asyncio.gather(
            *[_fetch_shopify_native(u) for u in urls],
            return_exceptions=True,
        )
        for idx, result in zip(refetch_targets, results):
            if isinstance(result, Exception) or not isinstance(result, dict):
                continue
            p = products[idx]
            native_vendor = (result.get("vendor") or "").strip() or None
            native_type = (result.get("product_type") or "").strip() or None
            if not p.get("vendor") and native_vendor:
                p["vendor"] = native_vendor
            if not p.get("product_type") and native_type:
                p["product_type"] = native_type
            if native_vendor or native_type:
                refetch_count += 1

    # Step 3: vendor fallback to discovered brand name. By definition
    # every product on this domain belongs to this brand — vendor=None
    # is never the right shape.
    vendor_fallback_count = 0
    for p in products:
        if not p.get("vendor") and merchant_name:
            p["vendor"] = merchant_name
            vendor_fallback_count += 1

    # Step 4: brand-level category inference. Only fires when at least
    # one product still lacks product_type after the prior strategies.
    # Single Gemini call for the whole audit, applied to all products
    # missing a real product_type.
    inferred_category: Optional[str] = None
    products_missing_type = [p for p in products if not p.get("product_type")]
    if products_missing_type:
        from services.bd_brand_category_inferrer import infer_brand_category
        sample_titles = [
            (p.get("title") or "").strip()
            for p in products[:6]
            if p.get("title")
        ]
        try:
            inferred_category = await infer_brand_category(
                merchant_name,
                sample_titles,
                domain=domain,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bd_cold_start: brand-category inference raised for %r: %s",
                merchant_name, exc,
            )
            inferred_category = None
        if inferred_category:
            for p in products_missing_type:
                p["product_type"] = inferred_category

    diagnostics = {
        "enriched": True,
        "shopify_refetch_hits": refetch_count,
        "vendor_fallback_applied": vendor_fallback_count,
        "brand_category_inferred": inferred_category,
        "products_with_vendor": sum(1 for p in products if p.get("vendor")),
        "products_with_product_type": sum(1 for p in products if p.get("product_type")),
    }
    return products, diagnostics


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

    # PR-B: brand signals — Open Graph, Schema.org, social handles,
    # sitemap structure, robots, SEO completeness. Pure parsing on the
    # already-fetched homepage HTML; sitemap + robots fetched in
    # parallel inside collect_brand_signals.
    # PR-C: brand context — Gemini grounded queries for retail
    # presence, founder story, press coverage. 3 parallel calls,
    # ~20s each (so ~20s total with parallelism). Falls back to
    # None when GEMINI_API_KEY unset or all calls fail.
    # Both run in parallel with each other to minimize wall-time.
    # PR-D adds infer_social_intelligence — 4 Gemini grounded calls
    # for own TikTok/Instagram presence + KOL endorsements per
    # platform. The 5th competitive_comparison call is gated on
    # competitor_brands which the audit produces; not available at
    # this point, so caller passes None and that call is skipped.
    # All three brand-intelligence functions run in parallel.
    from services.bd_brand_signals import (
        collect_brand_signals, infer_brand_context, infer_social_intelligence,
    )
    brand_signals: Optional[Dict[str, Any]] = None
    brand_context: Optional[Dict[str, Any]] = None
    social_intelligence: Optional[Dict[str, Any]] = None
    try:
        brand_signals_task = collect_brand_signals(homepage_html, domain, base)
        brand_context_task = infer_brand_context(merchant_name, domain)
        # social intelligence needs detected_handles from brand_signals;
        # but we don't have it yet. Wait for brand_signals first, then
        # kick off social_intelligence with the result. To keep wall-
        # time low, run brand_signals + brand_context in parallel first;
        # social_intelligence runs after brand_signals lands so it can
        # use the detected social handles.
        bs_res, bc_res = await asyncio.gather(
            brand_signals_task, brand_context_task, return_exceptions=True,
        )
        if not isinstance(bs_res, Exception):
            brand_signals = bs_res
        else:
            logger.warning(
                "bd_cold_start: brand_signals collection failed for %s: %s",
                domain, bs_res,
            )
        if not isinstance(bc_res, Exception):
            brand_context = bc_res
        else:
            logger.warning(
                "bd_cold_start: brand_context inference failed for %s: %s",
                domain, bc_res,
            )
        detected_handles = (brand_signals or {}).get("social") if brand_signals else []
        social_intelligence = await infer_social_intelligence(
            merchant_name,
            domain,
            detected_handles=detected_handles,
            competitor_brands=None,  # post-audit hook can re-call with audit's competitors
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "bd_cold_start: brand intelligence gather failed for %s: %s",
            domain, exc,
        )

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

    # Enrich the audit-bound subset with vendor + product_type. Without
    # this, the category_visibility_test gets skipped (bd_report_service
    # gates on product_type) and templates 9/10/category-mode collapse
    # for lack of vendor. Three-strategy stack: Shopify-native refetch,
    # merchant_name vendor fallback, brand-level Gemini category
    # inference. See _enrich_audit_products for details.
    audit_products, enrichment_diagnostics = await _enrich_audit_products(
        audit_products,
        merchant_name=merchant_name,
        domain=domain,
    )

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
        # Vendor + product_type backfill diagnostics. Surfaces which
        # strategies fired so BD operators can verify category test
        # ran on real categorical data (not mock/inferred fallback).
        "enrichment": enrichment_diagnostics,
        # PR-B brand signals (Open Graph, Schema.org Organization,
        # AggregateRating, social handles, sitemap structure, robots
        # directives, SEO completeness score). None when extraction
        # raised — audit still runs without it.
        "brand_signals": brand_signals,
        # PR-C Gemini-grounded brand context: retail_presence,
        # founder_story, press_coverage. None when GEMINI_API_KEY
        # unset OR all 3 calls failed. Each sub-field independently
        # nullable (one call can succeed while others fail).
        "brand_context": brand_context,
        # PR-D Gemini-grounded social intelligence: own TikTok +
        # Instagram presence (followers, view counts), KOL/creator
        # endorsements per platform, competitive comparison (gated
        # on competitor_brands which the audit produces — this
        # initial call passes None so competitive is omitted; a
        # follow-up hook can re-call with audit-derived competitors).
        "social_intelligence": social_intelligence,
    }
