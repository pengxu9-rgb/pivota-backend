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
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as _pg_insert

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
    # Canonical catalog product_key (prod::merchant::platform::source) — keeps
    # url_audit seeds parseable by any tool that splits product_key on the
    # `prod::` prefix / `::` separator, exactly like Shopify/marketplace rows.
    # (Lazy import keeps this pure mapping module import-light, like the db.*
    # imports in upsert_audited_sku_to_index below.)
    from services.catalog_sync_service import make_catalog_product_key

    return {
        "merchant_id": merchant_id,
        "platform": PLATFORM_URL_AUDIT,
        "source_product_id": source_id,
        "product_key": make_catalog_product_key(
            merchant_id, PLATFORM_URL_AUDIT, source_id
        ),
        "title": title,
        "brand": brand,
        "content_key": make_content_key(brand, title),
        "canonical_url": pdp_url,
        "source_domain": _host(pdp_url) or None,
        "product_type": (audit_product.get("product_type") or "").strip() or None,
        "raw_title": audit_product.get("raw_title"),
        "attributes_raw": audit_product.get("attributes_raw") or {},
    }


def resolve_seed_vendor(
    *,
    fetched_vendor: Optional[str],
    declared_brand: Optional[str],
    fallback_brand: Optional[str] = None,
) -> Optional[str]:
    """The brand to attribute to a URL-audit seed (and the audit's vendor-anchored
    query). Precedence:

      1. An EXPLICITLY-declared brand wins — a store-less brand pointing at a
         RETAILER PDP (Amazon / Olive Young) knows its own brand, whereas that
         page's JSON-LD `vendor` is often the retailer or marketplace seller, not
         the brand. Letting it win is what makes "index my product from where
         I'm listed" attribute to the right brand (and content_key).
      2. Else the fetched vendor (authoritative for the brand's own Shopify PDP).
      3. Else a resolved fallback brand (derived from domain / business name).

    None when nothing resolves."""
    declared = (declared_brand or "").strip()
    if declared:
        return declared
    fetched = (fetched_vendor or "").strip()
    if fetched:
        return fetched
    fallback = (fallback_brand or "").strip()
    return fallback or None


def audit_intake_enabled() -> bool:
    """Flag: auto-seed the commerce index from audits. Default OFF — ships dark,
    enabled per-env after canary. (The upsert is best-effort regardless, so it
    can never break an audit; the flag governs whether we attempt it at all.)"""
    return os.getenv("ENABLE_AUDIT_INDEX_INTAKE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# Only the columns we set; everything else (catalog_track, truth_tier,
# readiness_tier, pdp_scope='unverified', sync_status, created_at, …) takes its
# server_default — and pdp_lifecycle_stage stays NULL, so a seed is NOT recalled
# /served until it graduates or is claimed.
_CATALOG_INSERT_COLUMNS = (
    "product_key", "merchant_id", "platform", "source_product_id",
    "title", "brand", "content_key", "canonical_url", "source_domain",
    "product_type",
)


async def upsert_audited_sku_to_index(
    merchant_id: str, audit_product: Dict[str, Any]
) -> Optional[str]:
    """Best-effort: upsert one audited product into catalog_products (the
    canonical index entity) keyed on product_key, then refresh its agent_pdp_view
    row. An OBSERVED, unclaimed seed. Returns the content_key (or product_key) on
    success, None otherwise. NEVER raises — it must not break a live audit."""
    fields = audit_product_to_index_fields(merchant_id, audit_product)
    if not fields:
        return None
    try:
        from db.catalog import catalog_products
        from db.database import database

        values = {k: fields.get(k) for k in _CATALOG_INSERT_COLUMNS}
        stmt = _pg_insert(catalog_products).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["product_key"],
            set_={
                "title": stmt.excluded.title,
                "brand": func.coalesce(stmt.excluded.brand, catalog_products.c.brand),
                "content_key": func.coalesce(
                    stmt.excluded.content_key, catalog_products.c.content_key
                ),
                "canonical_url": func.coalesce(
                    stmt.excluded.canonical_url, catalog_products.c.canonical_url
                ),
                "source_domain": func.coalesce(
                    stmt.excluded.source_domain, catalog_products.c.source_domain
                ),
                "product_type": func.coalesce(
                    stmt.excluded.product_type, catalog_products.c.product_type
                ),
                "updated_at": func.now(),
                "content_changed_at": func.now(),
            },
        )
        await database.execute(stmt)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the audit
        logger.warning(
            "upsert_audited_sku_to_index: catalog upsert failed for %s: %s",
            fields.get("product_key"), str(exc)[:200],
        )
        return None

    content_key = fields.get("content_key")
    if content_key:
        try:
            from services.agent_pdp_view_assembler import (
                refresh_agent_pdp_view_for_content_key,
            )

            await refresh_agent_pdp_view_for_content_key(
                content_key, refresh_source="url_audit_intake"
            )
        except Exception as exc:  # noqa: BLE001 — PDP refresh is best-effort
            logger.warning(
                "upsert_audited_sku_to_index: pdp refresh failed for %s: %s",
                content_key, str(exc)[:200],
            )
    return content_key or fields.get("product_key")
