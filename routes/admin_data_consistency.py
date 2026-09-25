"""
Admin endpoint to check and fix data consistency issues
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from pydantic import BaseModel
from db.database import database
from typing import Optional
import logging

logger = logging.getLogger(__name__)
# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# Anonymous read of any merchant's data state, and an anonymous WRITE:
# POST /admin/data/fix/{merchant_id}.
router = APIRouter(prefix="/admin/data", tags=["admin-data-consistency"], dependencies=[Depends(require_admin_or_key)])

class ConsistencyReport(BaseModel):
    merchant_id: str
    issues_found: list
    recommendations: list

@router.get("/check/{merchant_id}")
async def check_data_consistency(merchant_id: str):
    """
    Check data consistency for a merchant
    Reports discrepancies in orders, products, stores, etc.
    """
    try:
        issues = []
        recommendations = []
        
        # 1. Check orders count from different sources
        logger.info(f"Checking data consistency for {merchant_id}")
        
        # Direct count from orders table
        orders_count_query = """
            SELECT COUNT(*) as count
            FROM orders
            WHERE merchant_id = :merchant_id
        """
        orders_result = await database.fetch_one(orders_count_query, {"merchant_id": merchant_id})
        direct_orders_count = orders_result["count"] if orders_result else 0
        
        # 2. Check products cache
        products_count_query = """
            SELECT 
                platform,
                COUNT(*) as count,
                COUNT(CASE WHEN expires_at > NOW() THEN 1 END) as active,
                COUNT(CASE WHEN expires_at <= NOW() THEN 1 END) as expired
            FROM products_cache
            WHERE merchant_id = :merchant_id
            GROUP BY platform
        """
        products_by_platform = await database.fetch_all(products_count_query, {"merchant_id": merchant_id})
        
        # 3. Check stores
        stores_query = """
            SELECT 
                platform,
                name,
                domain,
                status,
                product_count,
                connected_at
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
        """
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        
        # 4. Check for duplicate stores (same domain, different merchant_id)
        duplicate_stores_query = """
            SELECT 
                ms1.merchant_id as merchant_id_1,
                ms2.merchant_id as merchant_id_2,
                ms1.platform,
                ms1.domain
            FROM merchant_stores ms1
            JOIN merchant_stores ms2 
                ON ms1.domain = ms2.domain 
                AND ms1.platform = ms2.platform 
                AND ms1.merchant_id != ms2.merchant_id
            WHERE ms1.merchant_id = :merchant_id
        """
        duplicates = await database.fetch_all(duplicate_stores_query, {"merchant_id": merchant_id})
        
        if duplicates and len(duplicates) > 0:
            issues.append({
                "type": "duplicate_stores",
                "severity": "high",
                "message": f"Found {len(duplicates)} stores connected to multiple merchants",
                "details": [dict(d) for d in duplicates]
            })
            recommendations.append("Run /admin/cleanup/duplicate-stores to remove duplicates")
        
        # 5. Check product count mismatch
        for platform_data in products_by_platform:
            platform = platform_data["platform"]
            cache_count = platform_data["active"]
            
            # Find matching store
            matching_store = None
            for store in stores:
                if store["platform"] == platform:
                    matching_store = store
                    break
            
            if matching_store:
                store_product_count = matching_store["product_count"] or 0
                if cache_count != store_product_count:
                    issues.append({
                        "type": "product_count_mismatch",
                        "severity": "medium",
                        "platform": platform,
                        "cache_count": cache_count,
                        "store_count": store_product_count,
                        "message": f"{platform}: cache has {cache_count} products but store reports {store_product_count}"
                    })
                    recommendations.append(f"Sync {platform} products to update counts")
        
        # 6. Check for expired products
        total_expired = sum(p["expired"] for p in products_by_platform)
        if total_expired > 0:
            issues.append({
                "type": "expired_products",
                "severity": "low",
                "count": total_expired,
                "message": f"{total_expired} products have expired in cache"
            })
            recommendations.append("Run product sync to refresh expired products")
        
        return {
            "merchant_id": merchant_id,
            "summary": {
                "total_orders": direct_orders_count,
                "total_stores": len(stores),
                "products_by_platform": [
                    {
                        "platform": p["platform"],
                        "active": p["active"],
                        "expired": p["expired"]
                    }
                    for p in products_by_platform
                ],
                "stores": [
                    {
                        "platform": s["platform"],
                        "name": s["name"],
                        "status": s["status"],
                        "product_count": s["product_count"]
                    }
                    for s in stores
                ]
            },
            "issues_found": len(issues),
            "issues": issues,
            "recommendations": recommendations
        }
        
    except Exception as e:
        logger.error(f"Consistency check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fix/{merchant_id}")
async def auto_fix_consistency(merchant_id: str):
    """
    Automatically fix common data consistency issues
    """
    try:
        fixes_applied = []
        
        # 1. Update product counts in merchant_stores based on products_cache
        update_query = """
            UPDATE merchant_stores ms
            SET product_count = (
                SELECT COUNT(*)
                FROM products_cache pc
                WHERE pc.merchant_id = ms.merchant_id
                    AND pc.platform = ms.platform
                    AND (pc.expires_at IS NULL OR pc.expires_at > NOW())
            )
            WHERE ms.merchant_id = :merchant_id
        """
        await database.execute(update_query, {"merchant_id": merchant_id})
        fixes_applied.append("Updated product counts in merchant_stores")
        
        # 2. Remove expired products from cache
        delete_expired_query = """
            DELETE FROM products_cache
            WHERE merchant_id = :merchant_id
                AND expires_at IS NOT NULL
                AND expires_at <= NOW()
        """
        result = await database.execute(delete_expired_query, {"merchant_id": merchant_id})
        fixes_applied.append(f"Removed expired products from cache")
        
        return {
            "success": True,
            "merchant_id": merchant_id,
            "fixes_applied": fixes_applied
        }
        
    except Exception as e:
        logger.error(f"Auto-fix failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))




