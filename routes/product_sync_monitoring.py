"""
Product Sync Monitoring & Metrics
Tracks sync success rates, product counts, and platform health
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, List
from datetime import datetime, timedelta
from db.database import database
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products/monitoring", tags=["product-monitoring"])

@router.get("/sync-stats")
async def get_sync_statistics(
    current_user: dict = Depends(get_current_user)
):
    """
    Get overall product sync statistics
    """
    try:
        # Get sync stats by platform
        platform_stats_query = """
            SELECT 
                platform,
                COUNT(*) as total_products,
                COUNT(CASE WHEN expires_at > NOW() THEN 1 END) as active_products,
                COUNT(CASE WHEN expires_at <= NOW() THEN 1 END) as expired_products,
                MAX(cached_at) as last_sync,
                AVG(access_count) as avg_access_count
            FROM products_cache
            GROUP BY platform
        """
        
        platform_stats = await database.fetch_all(platform_stats_query)
        
        # Get merchant stats
        merchant_stats_query = """
            SELECT 
                COUNT(DISTINCT merchant_id) as total_merchants,
                COUNT(*) as total_products,
                MAX(cached_at) as last_overall_sync
            FROM products_cache
            WHERE expires_at > NOW()
        """
        
        merchant_stats = await database.fetch_one(merchant_stats_query)
        
        # Get sync performance (from merchant_stores)
        store_health_query = """
            SELECT 
                platform,
                COUNT(*) as connected_stores,
                COUNT(CASE WHEN status = 'active' THEN 1 END) as active_stores,
                AVG(EXTRACT(EPOCH FROM (NOW() - last_sync))) as avg_time_since_sync
            FROM merchant_stores
            GROUP BY platform
        """
        
        store_health = await database.fetch_all(store_health_query)
        
        return {
            "status": "success",
            "timestamp": datetime.now().isoformat(),
            "platform_stats": [
                {
                    "platform": p["platform"],
                    "total_products": p["total_products"],
                    "active_products": p["active_products"],
                    "expired_products": p["expired_products"],
                    "last_sync": p["last_sync"].isoformat() if p["last_sync"] else None,
                    "avg_access_count": float(p["avg_access_count"]) if p["avg_access_count"] else 0
                }
                for p in platform_stats
            ],
            "overall_stats": {
                "total_merchants": merchant_stats["total_merchants"] if merchant_stats else 0,
                "total_products": merchant_stats["total_products"] if merchant_stats else 0,
                "last_sync": merchant_stats["last_overall_sync"].isoformat() if merchant_stats and merchant_stats["last_overall_sync"] else None
            },
            "store_health": [
                {
                    "platform": s["platform"],
                    "connected_stores": s["connected_stores"],
                    "active_stores": s["active_stores"],
                    "avg_hours_since_sync": round(float(s["avg_time_since_sync"]) / 3600, 2) if s["avg_time_since_sync"] else None
                }
                for s in store_health
            ]
        }
        
    except Exception as e:
        logger.error(f"Error fetching sync stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/merchant/{merchant_id}/sync-history")
async def get_merchant_sync_history(
    merchant_id: str,
    days: int = 7,
    current_user: dict = Depends(get_current_user)
):
    """
    Get sync history for a specific merchant
    """
    try:
        # Get sync events from merchant_stores
        since = datetime.now() - timedelta(days=days)
        
        query = """
            SELECT 
                platform,
                last_sync,
                status,
                (
                    SELECT COUNT(*) 
                    FROM products_cache 
                    WHERE products_cache.merchant_id = merchant_stores.merchant_id 
                        AND products_cache.platform = merchant_stores.platform
                        AND expires_at > NOW()
                ) as current_product_count
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
                AND last_sync >= :since
            ORDER BY last_sync DESC
        """
        
        sync_events = await database.fetch_all(query, {
            "merchant_id": merchant_id,
            "since": since
        })
        
        return {
            "merchant_id": merchant_id,
            "period_days": days,
            "sync_events": [
                {
                    "platform": e["platform"],
                    "synced_at": e["last_sync"].isoformat() if e["last_sync"] else None,
                    "status": e["status"],
                    "product_count": e["current_product_count"]
                }
                for e in sync_events
            ]
        }
        
    except Exception as e:
        logger.error(f"Error fetching sync history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health-check")
async def sync_health_check():
    """
    Quick health check for product sync system (no auth required)
    """
    try:
        # Check if products_cache table is accessible
        test_query = "SELECT COUNT(*) as count FROM products_cache LIMIT 1"
        result = await database.fetch_one(test_query)
        
        # Check for stale data (no sync in last 24 hours)
        stale_check = """
            SELECT COUNT(DISTINCT merchant_id) as stale_merchants
            FROM merchant_stores
            WHERE status = 'active' 
                AND (last_sync IS NULL OR last_sync < NOW() - INTERVAL '24 hours')
        """
        stale_result = await database.fetch_one(stale_check)
        
        return {
            "status": "healthy",
            "database_accessible": True,
            "total_cached_products": result["count"] if result else 0,
            "stale_merchants": stale_result["stale_merchants"] if stale_result else 0,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }




