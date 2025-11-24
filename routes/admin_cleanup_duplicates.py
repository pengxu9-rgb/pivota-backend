"""
Admin endpoint to cleanup duplicate store connections
"""
from fastapi import APIRouter, HTTPException
from db.database import database
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/cleanup", tags=["admin-cleanup"])

@router.post("/duplicate-stores")
async def cleanup_duplicate_stores():
    """
    Clean up duplicate store connections (no auth for emergency)
    Keeps stores for merch_6b90dc9838d5fd9c (official demo)
    Removes duplicates for merch_208139f7600dbf42 (test account)
    """
    try:
        # Check current state
        check_query = """
            SELECT 
                merchant_id,
                COUNT(*) as store_count,
                STRING_AGG(DISTINCT platform, ', ') as platforms
            FROM merchant_stores
            WHERE merchant_id IN ('merch_6b90dc9838d5fd9c', 'merch_208139f7600dbf42')
            GROUP BY merchant_id
        """
        
        before_state = await database.fetch_all(check_query)
        logger.info(f"Before cleanup: {[dict(r) for r in before_state]}")
        
        # Delete duplicate stores for test merchant
        await database.execute(
            "DELETE FROM merchant_stores WHERE merchant_id = 'merch_208139f7600dbf42'"
        )
        
        # Delete products cache for test merchant
        await database.execute(
            "DELETE FROM products_cache WHERE merchant_id = 'merch_208139f7600dbf42'"
        )
        
        # Check after cleanup
        after_state = await database.fetch_all(check_query)
        logger.info(f"After cleanup: {[dict(r) for r in after_state]}")
        
        return {
            "success": True,
            "message": "Duplicate stores cleaned up",
            "before": [dict(r) for r in before_state],
            "after": [dict(r) for r in after_state],
            "kept_merchant": "merch_6b90dc9838d5fd9c",
            "removed_from": "merch_208139f7600dbf42"
        }
        
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check-duplicates")
async def check_duplicate_stores():
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




