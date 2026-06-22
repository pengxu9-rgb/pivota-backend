"""Audit -> commerce-index SKU intake (the AUTOMATIC index-population path).

When a merchant runs an audit on a product URL (the storefront-agnostic URL
audit), the fetched product info should generate/update a canonical SKU in the
commerce index as an OBSERVED, unclaimed seed. The brand can later CLAIM +
verify + attest (the manual / lab-evidence path) to upgrade it to
brand-attested.

This module maps a fetched audit-product dict (from
services.bd_cold_start_service.fetch_curated_audit_product:
``{title, raw_title, pdp_url, vendor, product_type, attributes_raw}``) into the
canonical catalog_products shape, keyed by content_key. The mapping here is
PURE + unit-tested; the DB upsert that consumes it (follow-up) must be
best-effort + flag-gated so it can never break a live audit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from services.catalog_identity import make_content_key

logger = logging.getLogger(__name__)

# Synthetic platform for URL-audit-sourced SKUs (no real storefront sync) — keeps
# them identifiable + de-conflated from Shopify/marketplace-synced rows.
PLATFORM_URL_AUDIT = "url_audit"


def _host(url: Optional[str]) -> str:
    if not url:
        return ""
    netloc = (urlparse(url).netloc or "").lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def stable_source_id(pdp_url: Optional[str]) -> str:
    """Stable source_product_id for a URL-sourced SKU: scheme/query/fragment
    stripped, lowercased host+path. Re-auditing the same URL updates the SAME
    row instead of duplicating it in the index."""
    if not pdp_url:
        return ""
    parsed = urlparse(pdp_url.strip())
    host = _host(pdp_url)
    path = (parsed.path or "").rstrip("/").lower()
    return f"{host}{path}" if host else pdp_url.strip().lower()


def audit_product_to_index_fields(
    merchant_id: str, audit_product: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Map a fetched audit-product dict -> canonical catalog_products fields.

    Returns None when there's not enough to mint an identity (no title, or no
    resolvable URL identity). content_key is brand+title here (GTIN enrichment
    is a follow-up); the deliberately non-unique key is de-conflated downstream
    by the identity gate, exactly as for other seed sources."""
    if not merchant_id or not isinstance(audit_product, dict):
        return None
    title = (audit_product.get("title") or "").strip()
    if not title:
        return None
    pdp_url = (audit_product.get("pdp_url") or "").strip() or None
    source_id = stable_source_id(pdp_url)
    if not source_id:
        return None
    brand = (audit_product.get("vendor") or "").strip() or None
    return {
        "merchant_id": merchant_id,
        "platform": PLATFORM_URL_AUDIT,
        "source_product_id": source_id,
        "product_key": f"{merchant_id}|{PLATFORM_URL_AUDIT}|{source_id}",
        "title": title,
        "brand": brand,
        "content_key": make_content_key(brand, title),
        "canonical_url": pdp_url,
        "source_domain": _host(pdp_url) or None,
        "product_type": (audit_product.get("product_type") or "").strip() or None,
        "raw_title": audit_product.get("raw_title"),
        "attributes_raw": audit_product.get("attributes_raw") or {},
    }
