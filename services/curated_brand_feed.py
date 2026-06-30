"""Curated-brand-list feed — the CLEAN primary source for catalog coverage.

Given a curated list of brand storefront domains, enumerate their products via
Shopify's PUBLIC `/products.json` and turn each into a Path-C *validated record*
(the `{pdp, offers}` shape `ingestion.ingest_validated_record` consumes). The
brand's own storefront is the authoritative PDP, so this bypasses Gemini URL
resolution entirely — it's deterministic, cheap, and carries the brand's own
title/price/image, variant **barcode (GTIN)**, and tags. The records then ingest
as depositable canonical anchors via the existing FK-order executor.

This is the "crawl the brand before they integrate" engine for the (very common)
Shopify-hosted D2C brand. Non-Shopify domains return [] (fall back to the audit/
agent feed). PURE-ish: this module fetches public pages + builds records; the
caller runs `ingest_validated_jsonl` + `apply_ingest_plan` (gated).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("curated_brand_feed")

_UA = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"
_PER_PAGE = 250  # Shopify max


def _clean_domain(domain: str) -> str:
    d = str(domain or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").rstrip("/")
    return d.split("/")[0]


async def fetch_shopify_products(
    domain: str,
    *,
    max_products: int = 500,
    timeout_s: float = 15.0,
) -> List[Dict[str, Any]]:
    """Page through `https://{domain}/products.json`. Returns raw Shopify product
    dicts (up to max_products), or [] if the store isn't Shopify / errors."""
    host = _clean_domain(domain)
    if not host:
        return []
    out: List[Dict[str, Any]] = []
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            page = 1
            while len(out) < max_products:
                url = f"https://{host}/products.json?limit={_PER_PAGE}&page={page}"
                resp = await client.get(url)
                if resp.status_code != 200 or "application/json" not in (resp.headers.get("content-type") or ""):
                    break
                products = (resp.json() or {}).get("products") or []
                if not products:
                    break
                out.extend(products)
                if len(products) < _PER_PAGE:
                    break
                page += 1
    except Exception as exc:  # noqa: BLE001 — a brand site being down must not break the batch
        logger.debug("fetch_shopify_products failed for %s: %s", host, str(exc)[:160])
    return out[:max_products]


def _first(seq: Any) -> Optional[Dict[str, Any]]:
    return seq[0] if isinstance(seq, list) and seq and isinstance(seq[0], dict) else None


def shopify_product_to_record(
    product: Dict[str, Any],
    *,
    domain: str,
    category_path: str,
    brand_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Map one Shopify `/products.json` product → a Path-C validated record
    (`{pdp, offers}`). Returns None if it lacks a title/handle (not actionable).
    The brand storefront is the authoritative PDP, so the offer is brand-direct
    and carries the variant barcode (GTIN) when present."""
    if not isinstance(product, dict):
        return None
    host = _clean_domain(domain)
    title = str(product.get("title") or "").strip()
    handle = str(product.get("handle") or "").strip()
    if not title or not handle:
        return None
    brand = str(brand_override or product.get("vendor") or "").strip()
    if not brand:
        return None
    variant = _first(product.get("variants")) or {}
    image = _first(product.get("images")) or {}
    barcode = str(variant.get("barcode") or "").strip() or None
    raw_tags = product.get("tags")
    tags = (
        raw_tags
        if isinstance(raw_tags, list)
        else [t.strip() for t in str(raw_tags or "").split(",") if t.strip()]
    )
    canonical_url = f"https://{host}/products/{handle}"
    price = variant.get("price")
    return {
        "pdp": {
            "brand": brand,
            "product_name": title,
            "category_path": category_path,
            "attribute_summary": str(product.get("product_type") or "").strip(),
            "barcode": barcode,  # real GTIN when the brand fills it — strongest deposit basis
            "source_domain": host,
            "tags": tags,
        },
        "offers": [
            {
                "merchant_inferred": brand,
                "canonical_url": canonical_url,
                "destination_url": canonical_url,
                "image_url": str(image.get("src") or "").strip(),
                "price": price,
                "in_stock": bool(variant.get("available")),
                "validated_at": "shopify_products_json",
            }
        ],
    }


async def records_for_brand(
    *,
    domain: str,
    category_path: str,
    brand: Optional[str] = None,
    max_products: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch a curated brand's storefront and return Path-C validated records."""
    products = await fetch_shopify_products(domain, max_products=max_products)
    records: List[Dict[str, Any]] = []
    for p in products:
        rec = shopify_product_to_record(
            p, domain=domain, category_path=category_path, brand_override=brand
        )
        if rec:
            records.append(rec)
    return records
