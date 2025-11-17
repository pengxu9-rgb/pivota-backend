"""
Enhanced Product Routes that work with products_cache
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any
from datetime import datetime
from db.database import database
from utils.auth import get_current_user, can_access_merchant
from models.standard_product import StandardProduct, ProductListResponse, ProductStatus
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products/v2", tags=["products-v2"])


def _map_cache_row_to_standard_product(
    merchant_id: str,
    platform: str,
    product_data: Dict[str, Any],
) -> StandardProduct:
    """
    Map products_cache.product_data into StandardProduct.

    If the payload already looks like a StandardProduct (id/merchant_id/platform/price present),
    we trust it and construct directly. Otherwise we build a minimal StandardProduct from the
    cached fields (e.g. ShopifyProductDTO payload).
    """
    # Fast path: already in StandardProduct shape
    if all(k in product_data for k in ("id", "merchant_id", "platform", "price")):
        return StandardProduct(**product_data)

    # Fallback mapping for minimal Shopify-style payload
    shopify_id = product_data.get("shopify_id") or product_data.get("id")
    title = product_data.get("title") or ""
    variants_count = int(product_data.get("variants_count") or 0)
    images_count = int(product_data.get("images_count") or 0)
    vendor = product_data.get("vendor")
    product_type = product_data.get("product_type")
    status_raw = product_data.get("status") or ProductStatus.ACTIVE.value

    # Simple data completeness score (EPIC-4 baseline)
    score = 0.0
    if title:
        score += 0.4
    if images_count > 0:
        score += 0.3
    if variants_count > 0:
        score += 0.2
    if product_type or vendor:
        score += 0.1
    score = round(score, 2)

    mapped: Dict[str, Any] = {
        "id": str(shopify_id or ""),
        "platform": platform,
        "merchant_id": merchant_id,
        "title": title,
        "description": None,
        "vendor": vendor,
        "product_type": product_type,
        "tags": [],
        "price": 0.0,
        "compare_at_price": None,
        "currency": "USD",
        "inventory_quantity": 0,
        "sku": None,
        "barcode": None,
        "image_url": None,
        "images": [],
        "variants": [],
        "status": status_raw,
        "published_at": None,
        "created_at": product_data.get("created_at"),
        "updated_at": product_data.get("updated_at"),
        "platform_metadata": {"raw": product_data.get("raw")},
        "data_completeness_score": score,
    }

    return StandardProduct(**mapped)

@router.get("/{merchant_id}", response_model=ProductListResponse)
async def get_merchant_products_v2(
    merchant_id: str,
    limit: int = Query(50, ge=1, le=500),
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
    4. Proper authentication with merchant access control
    """
    # Check merchant access
    try:
        logger.info(f"[V2] Fetching products for merchant {merchant_id}, user_role={current_user.get('role')}, user_merchant_id={current_user.get('merchant_id')}")
        
        if not can_access_merchant(current_user, merchant_id):
            logger.warning(f"[V2] Access denied for user {current_user.get('email')}")
            raise HTTPException(
                status_code=403,
                detail=f"Not authorized. Your merchant_id: {current_user.get('merchant_id')}, Requested: {merchant_id}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[V2] Error in access check: {e}")
        raise HTTPException(status_code=500, detail=f"Access check failed: {str(e)}")
    
    try:
        logger.info(f"Fetching products for merchant {merchant_id}, platform={platform}")
        
        # Build query - use raw string queries
        query_conditions = ["merchant_id = :merchant_id"]
        query_params = {"merchant_id": merchant_id}
        
        if platform:
            query_conditions.append("platform = :platform")
            query_params["platform"] = platform
        
        # Add expiration check using expires_at column
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
        
        # Get products - use direct formatting for LIMIT/OFFSET (safe because they're validated integers)
        products_query = f"""
            SELECT 
                product_data,
                platform,
                cached_at,
                expires_at
            FROM products_cache
            WHERE {where_clause}
            ORDER BY platform, cached_at DESC
            LIMIT {limit} OFFSET {offset}
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
                
                # Create StandardProduct (supports minimal Shopify payloads)
                product = _map_cache_row_to_standard_product(
                    merchant_id=merchant_id,
                    platform=row["platform"],
                    product_data=product_data,
                )
                products.append(product)
            except Exception as e:
                logger.error(f"Failed to parse product: {e}")
                continue
        
        logger.info(f"Found {len(products)} products for merchant {merchant_id}")
        
        # Determine platform for response (use first product's platform or "all")
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
        import traceback
        logger.error(f"[V2] Error fetching products: {e}")
        logger.error(f"[V2] Traceback: {traceback.format_exc()}")
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
    # Check merchant access
    if not can_access_merchant(current_user, merchant_id):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this merchant's data"
        )
    
    logger.info(f"[V2] Getting platforms for merchant {merchant_id}")
    
    try:
        query = """
            SELECT DISTINCT 
                platform,
                COUNT(*) as product_count,
                MAX(cached_at) as last_sync
            FROM products_cache
            WHERE merchant_id = :merchant_id
                AND (expires_at IS NULL OR expires_at > NOW())
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
