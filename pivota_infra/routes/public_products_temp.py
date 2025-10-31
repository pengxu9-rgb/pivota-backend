"""
Completely public products endpoint for testing - NO dependencies at all
"""
from fastapi import APIRouter, Query
from db.database import database
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["public-test"])

@router.get("/products/{merchant_id}")
async def get_products_public(
    merchant_id: str,
    limit: int = Query(100, ge=1, le=500)
):
    """
    PUBLIC endpoint - NO AUTH - for debugging only
    Returns products from cache
    """
    try:
        logger.info(f"[PUBLIC] Getting products for {merchant_id}")
        
        query = """
            SELECT 
                product_data,
                platform,
                cached_at
            FROM products_cache
            WHERE merchant_id = :merchant_id
                AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY platform, cached_at DESC
            LIMIT :limit
        """
        
        rows = await database.fetch_all(query, {"merchant_id": merchant_id, "limit": limit})
        
        logger.info(f"[PUBLIC] Found {len(rows)} products")
        
        products = []
        for row in rows:
            product_data = row["product_data"]
            if isinstance(product_data, str):
                import json
                product_data = json.loads(product_data)
            products.append(product_data)
        
        return {
            "success": True,
            "merchant_id": merchant_id,
            "total": len(products),
            "products": products,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"[PUBLIC] Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "merchant_id": merchant_id
        }


@router.get("/test")
async def public_test():
    """Ultimate simple test - just returns OK"""
    return {"status": "ok", "message": "Public endpoint works!", "timestamp": datetime.now().isoformat()}
