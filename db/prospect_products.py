"""
BD cold-start audit — prospect_products accessors.

Separate from catalog_products: prospect data is tentative (brand
hasn't onboarded yet). When a brand later signs up, claim_prospect_
products links the prospect rows to the new merchant_id.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def upsert_discovered_products(
    *,
    prospect_brand: str,
    prospect_domain: str,
    discovery_source: str,
    products: List[Dict[str, Any]],
    audit_run_id: Optional[str] = None,
) -> int:
    """Bulk upsert discovered products. Each product dict carries
    `{title, pdp_url, vendor?, product_type?}` (the audit shape) plus
    optional `raw_extracted` for the full source payload.

    Returns the count of rows upserted. Best-effort — DB failure
    doesn't fail the audit; we log and return 0.
    """
    from db.database import database
    if not products:
        return 0
    rows_inserted = 0
    now = datetime.now(timezone.utc)
    for p in products:
        url = (p.get("pdp_url") or "").strip()
        title = (p.get("title") or "").strip() or None
        if not url:
            continue
        raw = p.get("raw_extracted")
        raw_json = json.dumps(raw) if raw is not None else None
        try:
            await database.execute(
                """
                INSERT INTO prospect_products (
                  prospect_brand, prospect_domain, url, title, vendor,
                  product_type, discovery_source, raw_extracted,
                  discovered_at, last_audit_run_id, last_audited_at
                )
                VALUES (
                  :brand, :domain, :url, :title, :vendor, :product_type,
                  :source, CAST(:raw AS JSONB), :discovered_at,
                  :audit_run_id, :audited_at
                )
                ON CONFLICT (prospect_domain, url) DO UPDATE SET
                  prospect_brand = EXCLUDED.prospect_brand,
                  title = COALESCE(EXCLUDED.title, prospect_products.title),
                  vendor = COALESCE(EXCLUDED.vendor, prospect_products.vendor),
                  product_type = COALESCE(EXCLUDED.product_type, prospect_products.product_type),
                  discovery_source = EXCLUDED.discovery_source,
                  raw_extracted = COALESCE(EXCLUDED.raw_extracted, prospect_products.raw_extracted),
                  last_audit_run_id = COALESCE(EXCLUDED.last_audit_run_id, prospect_products.last_audit_run_id),
                  last_audited_at = COALESCE(EXCLUDED.last_audited_at, prospect_products.last_audited_at)
                """,
                {
                    "brand": prospect_brand or "",
                    "domain": prospect_domain or "",
                    "url": url,
                    "title": title,
                    "vendor": p.get("vendor"),
                    "product_type": p.get("product_type"),
                    "source": discovery_source,
                    "raw": raw_json,
                    "discovered_at": now,
                    "audit_run_id": audit_run_id,
                    "audited_at": now if audit_run_id else None,
                },
            )
            rows_inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prospect_products upsert failed for %s: %s", url, exc,
            )
    return rows_inserted


async def list_prospects_for_domain(
    prospect_domain: str,
    *,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Returns recent prospects for one domain — used by the BD
    portal to show "we've audited this brand before, here's what
    we know"."""
    from db.database import database
    try:
        rows = await database.fetch_all(
            """
            SELECT prospect_brand, prospect_domain, url, title,
                   vendor, product_type, discovery_source,
                   discovered_at, last_audit_run_id, last_audited_at,
                   claimed_at, claimed_by_merchant_id
              FROM prospect_products
             WHERE prospect_domain = :domain
             ORDER BY discovered_at DESC
             LIMIT :limit
            """,
            {"domain": prospect_domain, "limit": limit},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "prospect_products list failed for domain=%s: %s",
            prospect_domain, exc,
        )
        return []
    return [dict(r) for r in rows or []]


async def claim_prospects_for_merchant(
    *,
    prospect_domain: str,
    merchant_id: str,
) -> int:
    """When a brand onboards as a real merchant, claim their
    prospect rows so future cold-audits won't double-discover
    them. Returns the count of rows claimed."""
    from db.database import database
    if not prospect_domain or not merchant_id:
        return 0
    try:
        result = await database.fetch_one(
            """
            WITH updated AS (
              UPDATE prospect_products
                 SET claimed_at = NOW(),
                     claimed_by_merchant_id = :merchant_id
               WHERE prospect_domain = :domain
                 AND claimed_at IS NULL
              RETURNING 1
            )
            SELECT COUNT(*) AS claimed FROM updated
            """,
            {"domain": prospect_domain, "merchant_id": merchant_id},
        )
        return int((result or {}).get("claimed") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "prospect_products claim failed for domain=%s merchant=%s: %s",
            prospect_domain, merchant_id, exc,
        )
        return 0
