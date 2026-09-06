"""
Products cache maintenance endpoints (deduplication/compaction)
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from pydantic import BaseModel
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
# POST /admin/products/compact/{merchant_id} anonymously DELETEd
# products_cache rows for any merchant.
router = APIRouter(prefix="/admin/products", tags=["admin-products-maintenance"], dependencies=[Depends(require_admin_or_key)])

class CompactResult(BaseModel):
    merchant_id: str
    duplicates_removed: int
    note: str

@router.post("/compact/{merchant_id}", response_model=CompactResult)
async def compact_products_cache(merchant_id: str):
    """
    Deduplicate products_cache for a merchant, by (platform, product_id or sku).
    Keeps the most recent (cached_at) row and removes older duplicates.
    """
    try:
        # Remove rows where product_data is null (safety)
        await database.execute(
            """
            DELETE FROM products_cache
            WHERE merchant_id = :m AND product_data IS NULL
            """,
            {"m": merchant_id},
        )

        # Delete duplicates by JSON id first
        delete_by_id = await database.execute(
            """
            WITH ranked AS (
              SELECT ctid, ROW_NUMBER() OVER (
                PARTITION BY merchant_id, platform, (product_data->>'id')
                ORDER BY cached_at DESC NULLS LAST
              ) AS rn
              FROM products_cache
              WHERE merchant_id = :m AND (product_data->>'id') IS NOT NULL
            )
            DELETE FROM products_cache p
            USING ranked r
            WHERE p.ctid = r.ctid AND r.rn > 1
            RETURNING 1
            """,
            {"m": merchant_id},
        )

        # Delete duplicates by JSON sku when id is absent
        delete_by_sku = await database.execute(
            """
            WITH ranked AS (
              SELECT ctid, ROW_NUMBER() OVER (
                PARTITION BY merchant_id, platform, (product_data->>'sku')
                ORDER BY cached_at DESC NULLS LAST
              ) AS rn
              FROM products_cache
              WHERE merchant_id = :m AND (product_data->>'id') IS NULL AND (product_data->>'sku') IS NOT NULL
            )
            DELETE FROM products_cache p
            USING ranked r
            WHERE p.ctid = r.ctid AND r.rn > 1
            RETURNING 1
            """,
            {"m": merchant_id},
        )

        # Count removed: database.execute returns last status; we can't easily count here without RETURNING rows.
        # Provide a generic note; user can re-check counts via /products/v2
        removed = 0
        try:
            removed += int(delete_by_id or 0)
            removed += int(delete_by_sku or 0)
        except Exception:
            pass

        return CompactResult(
            merchant_id=merchant_id,
            duplicates_removed=removed,
            note="Duplicates compacted by id/sku; verify via /products/v2",
        )
    except Exception as e:
        logger.error(f"Compaction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))




