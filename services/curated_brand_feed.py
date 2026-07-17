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

import html
import logging
import re
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


# <script>/<style> INNER TEXT is code, not prose — page-builder exports
# (PageFly/GemPages) routinely embed style blocks in body_html; a naive
# tag-strip would keep the CSS soup and could auto-publish it as the brand's
# words. Strip whole blocks (and comments) BEFORE the tag pass.
_HTML_BLOCK_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>|<!--.*?-->")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
BODY_TEXT_MAX_LEN = 2000  # PDP prose field, not a document store


def body_html_to_text(body_html: Optional[str]) -> str:
    """Deterministic Shopify body_html → plain text: drop script/style/comment
    blocks, strip tags, unescape entities, collapse whitespace, cap at a word
    boundary. Output is the brand's own words or ''."""
    if not body_html:
        return ""
    text = _HTML_BLOCK_RE.sub(" ", str(body_html))
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > BODY_TEXT_MAX_LEN:
        cut = text[:BODY_TEXT_MAX_LEN]
        text = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return text


def _to_float(value: Any) -> Optional[float]:
    """Coerce Shopify's string prices (e.g. '56.00') to float; None if absent/invalid.
    Numeric columns (catalog_offers.*_price, external_product_seeds.price_amount) reject
    strings, so the mapper must hand downstream a real number or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    variants = product.get("variants")
    variants = variants if isinstance(variants, list) else []
    # Pick the first sellable (positive-price) variant. Gift-with-purchase and other
    # $0/unpriced items have no purchasable offer — drop the product entirely so it
    # never enters the commerce index (these were landing as junk PDPs/seeds, the
    # offers_skipped noise seen onboarding kosas).
    variant = None
    price = None
    for v in variants:
        p = _to_float((v or {}).get("price"))
        if p is not None and p > 0:
            variant, price = v, p
            break
    if variant is None:
        return None
    image = _first(product.get("images")) or {}
    barcode = str(variant.get("barcode") or "").strip() or None
    raw_tags = product.get("tags")
    tags = (
        raw_tags
        if isinstance(raw_tags, list)
        else [t.strip() for t in str(raw_tags or "").split(",") if t.strip()]
    )
    canonical_url = f"https://{host}/products/{handle}"
    return {
        "pdp": {
            "brand": brand,
            "product_name": title,
            "category_path": category_path,
            # Brand-authored body copy when present (it becomes the row's
            # description and feeds the lifecycle candidate gate + taxonomy
            # extractors); product_type alone otherwise. Rows minted without
            # body copy land 'draft' and rely on the description backfill /
            # LLM enrichment lane to promote.
            "attribute_summary": (
                body_html_to_text(product.get("body_html"))
                or str(product.get("product_type") or "").strip()
            ),
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
