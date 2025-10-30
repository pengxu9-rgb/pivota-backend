"""
Enhanced Product Routes that work with products_cache
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any
from datetime import datetime
from db.database import database
from utils.auth import get_current_user
from models.standard_product import StandardProduct, ProductListResponse
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products/v2", tags=["products-v2"])

@router.get("/{merchant_id}", response_model=ProductListResponse)
async def get_merchant_products_v2(
    merchant_id: str,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    platform: str = Query(None, description="Filter by platform"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get merchant products from cache (works for all platforms)
    
    This endpoint:
    1. Reads from products_cache table
    2. Supports all platforms (Shopify, Wix, WooCommerce, etc.)
    3. Returns standardized product format
    4. Does not require mcp_connected flag
    """
    try:
        logger.info(f"Fetching products for merchant {merchant_id}, platform={platform}")
        
        # Build query
        query_conditions = ["merchant_id = :merchant_id"]
        query_params = {"merchant_id": merchant_id, "limit": limit, "offset": offset}
        
        if platform:
            query_conditions.append("platform = :platform")
            query_params["platform"] = platform
        
        # Add TTL check - only get non-expired products
        query_conditions.append("(cached_at + INTERVAL '1 second' * ttl_seconds) > NOW()")
        
        where_clause = " AND ".join(query_conditions)
        
        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM products_cache
            WHERE {where_clause}
        """
        count_result = await database.fetch_one(count_query, query_params)
        total = count_result["total"] if count_result else 0
        
        # Get products
        products_query = f"""
            SELECT 
                product_data,
                platform,
                cached_at
            FROM products_cache
            WHERE {where_clause}
            ORDER BY cached_at DESC
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
                
                # Ensure platform is set
                if "platform" not in product_data:
                    product_data["platform"] = row["platform"]
                
                # Create StandardProduct
                product = StandardProduct(**product_data)
                products.append(product)
            except Exception as e:
                logger.error(f"Failed to parse product: {e}")
                continue
        
        logger.info(f"Found {len(products)} products for merchant {merchant_id}")
        
        return ProductListResponse(
            products=products,
            total=total,
            has_more=total > (offset + limit)
        )
        
    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch products: {str(e)}"
        )


@router.get("/{merchant_id}/platforms")
async def get_merchant_platforms(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all platforms that have cached products for this merchant"""
    try:
        query = """
            SELECT DISTINCT 
                platform,
                COUNT(*) as product_count,
                MAX(cached_at) as last_sync
            FROM products_cache
            WHERE merchant_id = :merchant_id
                AND (cached_at + INTERVAL '1 second' * ttl_seconds) > NOW()
            GROUP BY platform
            ORDER BY platform
        """
        
        platforms = await database.fetch_all(query, {"merchant_id": merchant_id})
        
        return {
            "merchant_id": merchant_id,
            "platforms": [
                {
                    "platform": p["platform"],
                    "product_count": p["product_count"],
                    "last_sync": p["last_sync"].isoformat() if p["last_sync"] else None
                }
                for p in platforms
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching platforms: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch platforms: {str(e)}"
        )
