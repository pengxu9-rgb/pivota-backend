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
from html import unescape
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from services import crawl_politeness

logger = logging.getLogger(__name__)

# Shopify product page URL pattern: any URL whose path ends in
# /products/{handle}. Used to gate native-JSON refetch (Shopify always
# exposes products.json at the same path with .json appended). Other
# storefronts use different patterns; we skip the refetch for them.
_SHOPIFY_PRODUCT_URL_RE = re.compile(r"/products/[A-Za-z0-9_-]+/?(?:\?.*)?$")
_SHOPIFY_REFETCH_TIMEOUT_S = 6.0
# Named because the politeness gate must be asked about the SAME agent we then send.
_BD_UA = "Pivota-BD-Audit/1.0"


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
            # Shared politeness gate; batch lane, so wait rather than drop the row.
            await crawl_politeness.before_request(url, user_agent=_BD_UA, max_wait=0)
            r = await client.get(
                url,
                headers={"Accept": "application/json", "User-Agent": _BD_UA},
            )
            crawl_politeness.note_response(
                url, r.status_code, retry_after=r.headers.get("retry-after")
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


# Generic (non-Shopify) PDP fetch: read clean product data straight from the
# page's structured markup — schema.org JSON-LD Product first (most reliable),
# OpenGraph title as a fallback. Used by the merchant-CURATED audit path, where
# the merchant hands us the exact product URL, so we fetch precisely it.
_PDP_FETCH_TIMEOUT_S = 8.0
_SEARCH_TITLE_UNIT_RE = (
    r"fl\s*oz|sticks?|tablets?|tabs?|capsules?|caps?|softgels?|"
    r"gummies|sachets?|servings?|ct|counts?|packs?|boxes|box|"
    r"bottles?|pouches?|bags?|kg|mg|ml|g|l|oz"
)
_LEADING_BRACKET_TAGS_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
_PARENTHETICAL_QUALIFIER_RE = re.compile(r"\([^)]*\)")
_TRAILING_MULTIPLIER_UNIT_RE = re.compile(
    rf"(?:[,\-\s]+|^)[x\u00d7]\s*\d+\s*(?:{_SEARCH_TITLE_UNIT_RE})\b\.?\s*$",
    re.IGNORECASE,
)
_TRAILING_NUM_UNIT_RE = re.compile(
    rf"(?:[,\-\s]+|^)\d+\s*(?:{_SEARCH_TITLE_UNIT_RE})\b\.?\s*$",
    re.IGNORECASE,
)
_DANGLING_MULTIPLIER_AFTER_UNIT_RE = re.compile(
    rf"\d+\s*(?:{_SEARCH_TITLE_UNIT_RE})\b\s*[x\u00d7]\s*$",
    re.IGNORECASE,
)
_TRAILING_MULTIPLIER_RE = re.compile(r"\s*[x\u00d7]\s*$", re.IGNORECASE)
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_OG_META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]*?'
    r'content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_OG_META_REV_RE = re.compile(  # content-before-property attribute ordering
    r'<meta[^>]+content=["\'](.*?)["\'][^>]*?'
    r'(?:property|name)=["\']og:title["\']',
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_TEXT_CAP = 2000
_ATTRIBUTE_BODY_HTML_CAP = 4000
_ATTRIBUTE_VARIANT_CAP = 20
_ATTRIBUTE_OPTION_VALUE_CAP = 25


def _clean_title_surface(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip(" \t\r\n,.;:-|/")


def _cap_text(value: Any, limit: int = _ATTRIBUTE_TEXT_CAP) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _strip_html_to_text(value: Any, limit: int = _ATTRIBUTE_TEXT_CAP) -> Optional[str]:
    """Keep product-copy evidence usable without adding a parser dependency."""
    raw = str(value or "").strip()
    if not raw:
        return None
    without_script = _HTML_SCRIPT_STYLE_RE.sub(" ", raw)
    without_tags = _HTML_TAG_RE.sub(" ", without_script)
    return _cap_text(unescape(without_tags), limit=limit)


def _normalize_tags(raw_tags: Any) -> List[str]:
    if isinstance(raw_tags, str):
        items = raw_tags.split(",")
    elif isinstance(raw_tags, list):
        items = raw_tags
    else:
        return []
    out: List[str] = []
    seen = set()
    for item in items:
        tag = _cap_text(item, limit=120)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _compact_shopify_variants(raw_variants: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_variants, list):
        return []
    out: List[Dict[str, Any]] = []
    for variant in raw_variants[:_ATTRIBUTE_VARIANT_CAP]:
        if not isinstance(variant, dict):
            continue
        row: Dict[str, Any] = {}
        for key in ("title", "price", "option1", "option2", "option3"):
            value = _cap_text(variant.get(key), limit=160)
            if value is not None:
                row[key] = value
        if "available" in variant:
            row["available"] = bool(variant.get("available"))
        if row:
            out.append(row)
    return out


def _compact_shopify_options(raw_options: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_options, list):
        return []
    out: List[Dict[str, Any]] = []
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        name = _cap_text(option.get("name"), limit=120)
        values_raw = option.get("values")
        values: List[str] = []
        if isinstance(values_raw, list):
            for item in values_raw[:_ATTRIBUTE_OPTION_VALUE_CAP]:
                value = _cap_text(item, limit=120)
                if value:
                    values.append(value)
        row: Dict[str, Any] = {}
        if name:
            row["name"] = name
        if values:
            row["values"] = values
        if row:
            out.append(row)
    return out


def _compact_shopify_images(raw_images: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_images, list):
        return None
    first_url: Optional[str] = None
    for image in raw_images:
        if isinstance(image, dict):
            first_url = _cap_text(
                image.get("src") or image.get("url") or image.get("original_src"),
                limit=500,
            )
        elif isinstance(image, str):
            first_url = _cap_text(image, limit=500)
        if first_url:
            break
    return {"count": len(raw_images), **({"first_url": first_url} if first_url else {})}


def _compact_jsonld_offers(raw_offers: Any) -> List[Dict[str, Any]]:
    offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
    out: List[Dict[str, Any]] = []
    for offer in offers[:_ATTRIBUTE_VARIANT_CAP]:
        if not isinstance(offer, dict):
            continue
        row: Dict[str, Any] = {}
        for key in ("price", "priceCurrency", "availability"):
            value = _cap_text(offer.get(key), limit=160)
            if value is not None:
                row[key] = value
        if row:
            out.append(row)
    return out


def _compact_jsonld_rating(raw_rating: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_rating, dict):
        return None
    row: Dict[str, Any] = {}
    for key in ("ratingValue", "reviewCount"):
        value = _cap_text(raw_rating.get(key), limit=80)
        if value is not None:
            row[key] = value
    return row or None


def _jsonld_brand_name(raw_brand: Any) -> Optional[str]:
    if isinstance(raw_brand, dict):
        return _cap_text(raw_brand.get("name"), limit=160)
    if isinstance(raw_brand, str):
        return _cap_text(raw_brand, limit=160)
    return None


def _shopify_attributes_raw(product: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {"source": "shopify_native"}
    tags = _normalize_tags(product.get("tags"))
    if tags:
        attrs["tags"] = tags
    body_html = _cap_text(product.get("body_html"), limit=_ATTRIBUTE_BODY_HTML_CAP)
    if body_html:
        attrs["body_html"] = body_html
        attrs["description"] = _strip_html_to_text(body_html)
    variants = _compact_shopify_variants(product.get("variants"))
    if variants:
        attrs["variants"] = variants
    options = _compact_shopify_options(product.get("options"))
    if options:
        attrs["options"] = options
    product_type = _cap_text(product.get("product_type"), limit=160)
    if product_type:
        attrs["product_type"] = product_type
    handle = _cap_text(product.get("handle"), limit=240)
    if handle:
        attrs["handle"] = handle
    images = _compact_shopify_images(product.get("images"))
    if images:
        attrs["images"] = images
    return attrs


def _pdp_attributes_raw(meta: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {"source": "pdp_metadata"}
    for key in ("description", "brand", "category", "product_type"):
        value = meta.get(key)
        if value is not None:
            attrs[key] = value
    offers = meta.get("offers")
    if offers:
        attrs["offers"] = offers
    rating = meta.get("aggregateRating")
    if rating:
        attrs["aggregateRating"] = rating
    return attrs


def _token_count(title: str) -> int:
    return len(re.findall(r"\S+", title or ""))


def _strip_trailing_pack_descriptors(title: str) -> str:
    cleaned = _clean_title_surface(title)
    while cleaned:
        before = cleaned
        cleaned = _TRAILING_MULTIPLIER_UNIT_RE.sub("", cleaned)
        cleaned = _clean_title_surface(cleaned)
        cleaned = _TRAILING_NUM_UNIT_RE.sub("", cleaned)
        cleaned = _clean_title_surface(cleaned)
        if _DANGLING_MULTIPLIER_AFTER_UNIT_RE.search(cleaned):
            cleaned = _TRAILING_MULTIPLIER_RE.sub("", cleaned)
            cleaned = _clean_title_surface(cleaned)
        if cleaned == before:
            break
    return cleaned


def _strip_site_suffix(title: str) -> str:
    """Drop a trailing ' | <site/retailer>' segment from a storefront title.

    Retail-channel pages title themselves "Brand Product … | OLIVE YOUNG Global"
    (or "… | Sephora", "… | Amazon.com"). Without stripping it, every generated
    buyer query carries the retailer name ("where can I buy … | OLIVE YOUNG
    Global"), which is noise. Conservative: keep the segment BEFORE the first
    ' | ' only when it's still a real title (>=2 tokens with letters); otherwise
    leave the title untouched."""
    parts = re.split(r"\s+\|\s+", title or "", maxsplit=1)
    if len(parts) > 1:
        head = parts[0].strip()
        if _token_count(head) >= 2 and any(ch.isalpha() for ch in head):
            return head
    return title or ""


def normalize_product_title_for_search(raw_title: str) -> str:
    """Turn a noisy storefront title into a conservative search query title."""
    original = raw_title or ""
    site_stripped = _strip_site_suffix(original)
    bracket_stripped = _clean_title_surface(
        _LEADING_BRACKET_TAGS_RE.sub("", site_stripped)
    )
    without_qualifiers = _clean_title_surface(
        _PARENTHETICAL_QUALIFIER_RE.sub("", bracket_stripped)
    )
    pack_stripped = _strip_trailing_pack_descriptors(without_qualifiers)

    if (
        pack_stripped != without_qualifiers
        and _token_count(pack_stripped) < 2
        and _token_count(without_qualifiers) >= 2
    ):
        cleaned = without_qualifiers
    else:
        cleaned = pack_stripped

    if len(cleaned) < 3 or not any(ch.isalpha() for ch in cleaned):
        return bracket_stripped or site_stripped or original
    return cleaned


def _walk_jsonld_for_product(node: Any) -> Optional[Dict[str, Any]]:
    """Depth-first search a parsed JSON-LD value for the first object whose
    @type is (or includes) 'Product'. Handles bare objects, arrays, and the
    common @graph wrapper. Returns the Product node or None."""
    if isinstance(node, list):
        for item in node:
            found = _walk_jsonld_for_product(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    raw_type = node.get("@type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if any(str(t).strip().lower() == "product" for t in types if t):
        return node
    graph = node.get("@graph")
    if graph is not None:
        return _walk_jsonld_for_product(graph)
    return None


async def _fetch_pdp_metadata(pdp_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a generic (non-Shopify) PDP's HTML and extract clean product data
    from schema.org JSON-LD (`Product`) with an OpenGraph title fallback.
    Returns {title, vendor, product_type} plus optional Product attributes, or
    None. Best-effort — the caller treats None as 'could not resolve a product
    at this URL'."""
    if not pdp_url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=_PDP_FETCH_TIMEOUT_S, follow_redirects=True,
        ) as client:
            r = await client.get(
                pdp_url,
                headers={
                    "User-Agent": "Pivota-BD-Audit/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.debug("pdp metadata fetch error for %s: %s", pdp_url, exc)
        return None
    if r.status_code != 200:
        return None
    html = r.text or ""

    title: Optional[str] = None
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    description: Optional[str] = None
    offers: List[Dict[str, Any]] = []
    aggregate_rating: Optional[Dict[str, Any]] = None
    brand_name: Optional[str] = None
    category_name: Optional[str] = None

    # 1. JSON-LD Product — the structured, authoritative source.
    for block in _JSON_LD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        product = _walk_jsonld_for_product(data)
        if not product:
            continue
        title = (str(product.get("name") or "")).strip() or None
        brand_name = _jsonld_brand_name(product.get("brand"))
        vendor = brand_name
        category = product.get("category")
        if isinstance(category, str):
            category_name = category.strip() or None
            product_type = category_name
        description = _cap_text(product.get("description"))
        offers = _compact_jsonld_offers(product.get("offers"))
        aggregate_rating = _compact_jsonld_rating(product.get("aggregateRating"))
        if title:
            break

    # 2. OpenGraph title fallback (handles either attribute ordering).
    if not title:
        m = _OG_META_RE.search(html) or _OG_META_REV_RE.search(html)
        if m:
            title = (m.group(1) or "").strip() or None

    if not title:
        return None
    return {
        "title": title,
        "vendor": vendor,
        "product_type": product_type,
        **({"description": description} if description else {}),
        **({"offers": offers} if offers else {}),
        **({"aggregateRating": aggregate_rating} if aggregate_rating else {}),
        **({"brand": brand_name} if brand_name else {}),
        **({"category": category_name} if category_name else {}),
    }


async def fetch_curated_audit_product(
    pdp_url: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch ONE merchant-provided product URL into a clean audit-product dict
    `{title, pdp_url, vendor, product_type}`.

    The merchant-CURATED audit path: the merchant chose this exact URL, so we
    fetch precisely it — no discovery, no crawl, no selection heuristics (the
    #1 source of bad audits). Shopify native `.json` first (richest: vendor +
    product_type + variants), then a generic JSON-LD/OpenGraph PDP read.

    Returns `(product, None)` on success, or `(None, reason)` when the URL
    can't be resolved to a real product so the caller can tell the merchant
    which link to fix.
    """
    url = (pdp_url or "").strip()
    if not url:
        return None, "empty URL"
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, (
            f"{pdp_url!r} is not a valid URL "
            "(it must start with http:// or https://)"
        )

    # 1. Shopify native .json — clean, structured, no auth, ~200-500ms.
    native = await _fetch_shopify_native(url)
    if native:
        raw_title = (str(native.get("title") or "")).strip()
        if raw_title:
            return {
                "title": normalize_product_title_for_search(raw_title),
                "raw_title": raw_title,
                "pdp_url": url,
                "vendor": (str(native.get("vendor") or "")).strip() or None,
                "product_type": (
                    (str(native.get("product_type") or "")).strip() or None
                ),
                "attributes_raw": _shopify_attributes_raw(native),
            }, None

    # 2. Generic PDP: schema.org JSON-LD Product + OpenGraph fallback.
    meta = await _fetch_pdp_metadata(url)
    if meta and meta.get("title"):
        raw_title = str(meta["title"]).strip()
        return {
            "title": normalize_product_title_for_search(raw_title),
            "raw_title": raw_title,
            "pdp_url": url,
            "vendor": meta.get("vendor"),
            "product_type": meta.get("product_type"),
            "attributes_raw": _pdp_attributes_raw(meta),
        }, None

    return None, (
        f"couldn't read a product from {url} — make sure the link opens a "
        "single product page (we read Shopify product JSON or the page's "
        "structured product data)"
    )


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


# PDP slug patterns that signal a draft / duplicate / test product
# the operator probably didn't intend to publish for AI-channel
# discovery. Conservative list — only flags patterns that are
# unambiguous markers of non-canonical SKUs.
#
# Reproduced the Grüns case: Shopify product duplication produced
# `gruns.co/products/gruns-copy` for a "Mother's Day Bundle - Adults
# + Kids" SKU. Catalog-intelligence returned this in the first slot;
# the audit ran against it and scored 0/0/0 even though Grüns'
# flagship Greens Gummies is famously cited by Forbes/Women's Health.
_SKETCHY_PDP_SLUG_SUFFIXES = (
    "-copy", "-clone", "-duplicate",
    "-draft", "-test", "-temp", "-archive", "-old", "-backup",
    "_copy", "_clone", "_duplicate",
    "_draft", "_test", "_temp", "_archive", "_old", "_backup",
)


def _has_sketchy_pdp_slug(product: Dict[str, Any]) -> bool:
    """Heuristic: does this product's PDP slug end with a Shopify-style
    duplication / draft suffix? Conservative — checks suffixes only,
    not arbitrary substring matches (so a genuine product named
    "vintage-test-tube-display" doesn't trigger).
    """
    pdp_url = product.get("pdp_url") or ""
    if not isinstance(pdp_url, str) or not pdp_url:
        return False
    # Extract the last path segment, lowercase, strip query / trailing
    # slash. /products/gruns-copy → gruns-copy; /products/gruns-copy/?utm=
    # → gruns-copy.
    path = pdp_url.split("?", 1)[0].rstrip("/")
    slug = path.rsplit("/", 1)[-1].lower()
    return any(slug.endswith(suf) for suf in _SKETCHY_PDP_SLUG_SUFFIXES)


def _demote_sketchy_pdp_slugs(
    products: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Move products with draft / duplicate slugs to the END of the
    candidate list. Doesn't drop them — if the entire catalog is
    sketchy slugs (e.g. a brand-new merchant whose only published
    products are drafts), the audit can still proceed against them
    rather than failing with no SKUs.

    Logs a structured warning when demotion fires so ops can spot
    catalogs where catalog-intelligence is over-prioritizing
    duplicate slugs (and so the BD operator knows the audit ran
    against the cleaner SKUs).
    """
    if not products:
        return products
    clean: List[Dict[str, Any]] = []
    sketchy: List[Dict[str, Any]] = []
    for p in products:
        if _has_sketchy_pdp_slug(p):
            sketchy.append(p)
        else:
            clean.append(p)
    if sketchy and clean:
        # Only log when we actually changed the order. If everything
        # is sketchy, the audit picks the same products it would have
        # anyway — no useful signal in logging.
        logger.info(
            "bd_cold_start: demoted %d product(s) with draft/duplicate "
            "PDP slugs (e.g. %s) to end of audit queue; %d clean "
            "product(s) take priority",
            len(sketchy),
            (sketchy[0].get("pdp_url") or "")[:120],
            len(clean),
        )
    return clean + sketchy


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
    #
    # First, demote draft / duplicate / test SKU slugs to the back of
    # the queue. Reproduces the Grüns failure: catalog-intelligence
    # returned "Mother's Day Bundle - Adults + Kids" with PDP slug
    # `gruns-copy` (a Shopify product duplication). The audit then
    # probed that obscure seasonal bundle instead of the flagship
    # Greens Gummies, returning 0/0/0 even though Forbes etc. cite
    # the flagship verbatim.
    #
    # Filter is conservative — moves sketchy slugs to the END of the
    # candidate list rather than dropping them. If the entire catalog
    # is sketchy slugs (rare but possible for a new merchant whose
    # only published products are drafts), the audit still runs.
    products = _demote_sketchy_pdp_slugs(products)
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
