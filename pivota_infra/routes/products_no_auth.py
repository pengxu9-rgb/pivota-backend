"""
Temporary products endpoint without strict auth for debugging
"""
from fastapi import APIRouter, Query, HTTPException
from typing import List, Dict, Any, Optional
from datetime import datetime
from db.database import database
from models.standard_product import StandardProduct, ProductListResponse
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products/debug", tags=["products-debug"])

@router.get("/{merchant_id}")
async def get_merchant_products_no_auth(
    merchant_id: str,
    limit: int = Query(250, ge=1, le=500),
    offset: int = Query(0, ge=0),
    platform: str = Query(None, description="Filter by platform")
):
    """
    Get merchant products from cache - NO AUTH CHECK (debugging only)
    This endpoint bypasses authentication to diagnose the 403 issue
    """
    try:
        logger.info(f"[NO_AUTH] Fetching products for merchant {merchant_id}, platform={platform}")
        
        # Build query
        query_conditions = ["merchant_id = :merchant_id"]
        query_params = {"merchant_id": merchant_id, "limit": limit, "offset": offset}
        
        if platform:
            query_conditions.append("platform = :platform")
            query_params["platform"] = platform
        
        # Add expiration check
        query_conditions.append("(expires_at IS NULL OR expires_at > NOW())")
        
        where_clause = " AND ".join(query_conditions)
        
        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM products_cache
            WHERE {where_clause}
        """
        count_result = await database.fetch_one(count_query, query_params)
        total = count_result["total"] if count_result else 0
        
        logger.info(f"[NO_AUTH] Found {total} products in cache")
        
        # Get products
        products_query = f"""
            SELECT 
                product_data,
                platform,
                cached_at,
                expires_at
            FROM products_cache
            WHERE {where_clause}
            ORDER BY platform, cached_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        rows = await database.fetch_all(products_query, query_params)
        
        # Parse products
        products = []
        for row in rows:
            try:
                product_data = row["product_data"]
                if isinstance(product_data, str):
                    product_data = json.loads(product_data)
                
                if "platform" not in product_data:
                    product_data["platform"] = row["platform"]
                
                product = StandardProduct(**product_data)
                products.append(product)
            except Exception as e:
                logger.error(f"[NO_AUTH] Failed to parse product: {e}")
                continue
        
        logger.info(f"[NO_AUTH] Returning {len(products)} products")
        
        # Determine platform for response
        response_platform = "all"
        if products and len(products) > 0:
            response_platform = products[0].platform
        elif platform:
            response_platform = platform
        
        return ProductListResponse(
            merchant_id=merchant_id,
            platform=response_platform,
            products=products,
            total=total,
            next_page_token=str(offset + limit) if total > (offset + limit) else None,
            fetched_at=datetime.now()
        )
        
    except Exception as e:
        logger.error(f"[NO_AUTH] Error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch products: {str(e)}"
        )


@router.get("/{merchant_id}/raw")
async def get_raw_cache_data(merchant_id: str):
    """Get raw cache data for debugging"""
    try:
        query = """
            SELECT 
                platform,
                COUNT(*) as total,
                COUNT(CASE WHEN expires_at > NOW() THEN 1 END) as active,
                COUNT(CASE WHEN expires_at <= NOW() THEN 1 END) as expired,
                MAX(cached_at) as last_cached
            FROM products_cache
            WHERE merchant_id = :merchant_id
            GROUP BY platform
        """
        
        rows = await database.fetch_all(query, {"merchant_id": merchant_id})
        
        return {
            "merchant_id": merchant_id,
            "cache_status": [dict(r) for r in rows]
        }
    except Exception as e:
        return {"error": str(e)}
