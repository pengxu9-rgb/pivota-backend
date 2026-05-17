"""
Admin endpoint to cleanup duplicate store connections
"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from routes.auth_routes import require_admin
from utils.runtime_safety import require_runtime_gate
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/cleanup", tags=["admin-cleanup"])

@router.post("/duplicate-stores")
async def cleanup_duplicate_stores(current_user: dict = Depends(require_admin)):
    """Clean up duplicate store connections, gated for production."""
    require_runtime_gate("ENABLE_DUPLICATE_STORE_CLEANUP")
    try:
        check_query = """
            SELECT
                domain,
                platform,
                COUNT(*) as store_count,
                STRING_AGG(DISTINCT merchant_id, ', ') as merchant_ids
            FROM merchant_stores
            WHERE COALESCE(NULLIF(trim(domain), ''), '') <> ''
            GROUP BY domain, platform
            HAVING COUNT(*) > 1
        """
        before_state = await database.fetch_all(check_query)
        logger.info(f"Before cleanup: {[dict(r) for r in before_state]}")

        deleted = await database.execute(
            """
            DELETE FROM merchant_stores
            WHERE store_id IN (
                SELECT store_id
                FROM (
                    SELECT
                        store_id,
                        row_number() OVER (
                            PARTITION BY merchant_id, lower(coalesce(platform, '')), lower(coalesce(domain, ''))
                            ORDER BY
                                CASE WHEN lower(coalesce(status, '')) IN ('active', 'connected') THEN 0 ELSE 1 END,
                                connected_at DESC NULLS LAST,
                                store_id DESC
                        ) AS rn
                    FROM merchant_stores
                ) ranked
                WHERE rn > 1
            )
            """
        )

        after_state = await database.fetch_all(check_query)
        logger.info(f"After cleanup: {[dict(r) for r in after_state]}")

        return {
            "success": True,
            "message": "Duplicate stores cleaned up",
            "before": [dict(r) for r in before_state],
            "after": [dict(r) for r in after_state],
            "deleted": deleted,
        }
        
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check-duplicates")
async def check_duplicate_stores(current_user: dict = Depends(require_admin)):
    """Check for duplicate store connections across merchants"""
    try:
        # Find stores with same domain connected to different merchants
        query = """
            SELECT 
                domain,
                platform,
                COUNT(DISTINCT merchant_id) as merchant_count,
                STRING_AGG(DISTINCT merchant_id, ', ') as merchant_ids
            FROM merchant_stores
            GROUP BY domain, platform
            HAVING COUNT(DISTINCT merchant_id) > 1
        """
        
        duplicates = await database.fetch_all(query)
        
        return {
            "duplicates_found": len(duplicates),
            "duplicates": [
                {
                    "platform": d["platform"],
                    "domain": d["domain"],
                    "merchant_count": d["merchant_count"],
                    "merchant_ids": d["merchant_ids"]
                }
                for d in duplicates
            ]
        }
    except Exception as e:
        return {"error": str(e)}


