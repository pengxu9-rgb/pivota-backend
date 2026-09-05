"""
Admin endpoints to canonicalize merchant data (merge duplicates into a canonical merchant_id)
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from db.database import database
import logging

logger = logging.getLogger(__name__)
# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# POST /admin/merchants/canonicalize rewrote merchant identity anonymously.
router = APIRouter(prefix="/admin/merchants", tags=["admin-merchants"], dependencies=[Depends(require_admin_or_key)])

class CanonicalizeRequest(BaseModel):
    canonical_merchant_id: str = Field(..., description="The merchant_id to keep and merge data into")
    merge_from: List[str] = Field(default_factory=list, description="Merchant IDs to merge from into the canonical one")
    dry_run: bool = Field(default=True, description="If true, only analyze and return the plan without making changes")

class CanonicalizeResult(BaseModel):
    success: bool
    message: str
    canonical_merchant_id: str
    merged_from: List[str]
    actions: List[str]
    counts_before: Dict[str, Any]
    counts_after: Optional[Dict[str, Any]] = None

async def _count_state(merchant_id: str) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    try:
        orders_count = await database.fetch_one(
            "SELECT COUNT(*) AS c FROM orders WHERE merchant_id = :m",
            {"m": merchant_id},
        )
        stores_count = await database.fetch_one(
            "SELECT COUNT(*) AS c FROM merchant_stores WHERE merchant_id = :m",
            {"m": merchant_id},
        )
        cache_total = await database.fetch_one(
            "SELECT COUNT(*) AS c FROM products_cache WHERE merchant_id = :m",
            {"m": merchant_id},
        )
        cache_active = await database.fetch_one(
            """
            SELECT COUNT(*) AS c
            FROM products_cache
            WHERE merchant_id = :m AND (expires_at IS NULL OR expires_at > NOW())
            """,
            {"m": merchant_id},
        )
        state = {
            "orders": (orders_count or {}).get("c", 0),
            "stores": (stores_count or {}).get("c", 0),
            "products_cache_total": (cache_total or {}).get("c", 0),
            "products_cache_active": (cache_active or {}).get("c", 0),
        }
    except Exception as e:
        logger.warning(f"Count state failed for {merchant_id}: {e}")
    return state

@router.post("/canonicalize", response_model=CanonicalizeResult)
async def canonicalize_merchants(payload: CanonicalizeRequest):
    """
    Merge data from `merge_from` merchant_ids into `canonical_merchant_id`.
    - Updates orders, products_cache, merchant_stores to point to the canonical merchant
    - Leaves original merchant_onboarding rows intact unless future cleanup is desired
    - If dry_run=True, returns planned actions and counts without modifying data
    """
    canonical = payload.canonical_merchant_id
    sources = [m for m in payload.merge_from if m and m != canonical]

    if not sources:
        return CanonicalizeResult(
            success=True,
            message="No source merchant_ids provided; nothing to merge",
            canonical_merchant_id=canonical,
            merged_from=[],
            actions=[],
            counts_before={canonical: await _count_state(canonical)},
            counts_after=None,
        )

    try:
        actions: List[str] = []
        counts_before: Dict[str, Any] = {canonical: await _count_state(canonical)}
        for s in sources:
            counts_before[s] = await _count_state(s)

        # Build SQL actions
        sql_actions = [
            ("orders", "UPDATE orders SET merchant_id = :canonical WHERE merchant_id = :src"),
            (
                "products_cache",
                "UPDATE products_cache SET merchant_id = :canonical WHERE merchant_id = :src",
            ),
            (
                "merchant_stores",
                "UPDATE merchant_stores SET merchant_id = :canonical WHERE merchant_id = :src",
            ),
        ]

        # Additional deduplication: remove exact duplicate store entries after merge (same platform+domain)
        dedupe_sql = (
            """
            DELETE FROM merchant_stores a
            USING merchant_stores b
            WHERE a.store_id <> b.store_id
              AND a.merchant_id = :canonical
              AND b.merchant_id = :canonical
              AND COALESCE(a.platform,'') = COALESCE(b.platform,'')
              AND COALESCE(a.domain,'') = COALESCE(b.domain,'')
              AND a.created_at < b.created_at
            """,
            "merchant_stores_dedupe",
        )

        planned = []
        for s in sources:
            for table, stmt in sql_actions:
                planned.append(f"{table}: {s} -> {canonical}")
        planned.append("merchant_stores: deduplicate canonical by platform+domain")

        if payload.dry_run:
            return CanonicalizeResult(
                success=True,
                message="Dry run: planned canonicalization",
                canonical_merchant_id=canonical,
                merged_from=sources,
                actions=planned,
                counts_before=counts_before,
                counts_after=None,
            )

        # Execute updates in order: orders -> products_cache -> merchant_stores -> dedupe
        for s in sources:
            for _, stmt in sql_actions:
                await database.execute(stmt, {"canonical": canonical, "src": s})
        # Deduplicate stores
        await database.execute(dedupe_sql[0], {"canonical": canonical})

        counts_after: Dict[str, Any] = {canonical: await _count_state(canonical)}
        for s in sources:
            counts_after[s] = await _count_state(s)

        return CanonicalizeResult(
            success=True,
            message="Canonicalization completed",
            canonical_merchant_id=canonical,
            merged_from=sources,
            actions=planned,
            counts_before=counts_before,
            counts_after=counts_after,
        )
    except Exception as e:
        logger.error(f"Canonicalization failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))




