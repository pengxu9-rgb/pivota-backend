"""
Enhanced Product Routes that work with products_cache
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any, Optional
from datetime import datetime
from db.database import database
from utils.auth import get_current_user, can_access_merchant
from models.standard_product import StandardProduct, ProductListResponse, ProductStatus, StandardProductVariant
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products/v2", tags=["products-v2"])


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _map_cache_row_to_standard_product(
    merchant_id: str,
    platform: str,
    product_data: Dict[str, Any],
) -> StandardProduct:
    """
    Map products_cache.product_data into StandardProduct.

    If the payload already looks like a StandardProduct (id/merchant_id/platform/price present),
    we trust it and construct directly. Otherwise we build a minimal StandardProduct from the
    cached fields (e.g. ShopifyProductDTO payload) and, when possible, derive key sellable
    fields from product_data.raw (variants/images).
    """
    # Fast path: already in StandardProduct shape
    if all(k in product_data for k in ("id", "merchant_id", "platform", "price")):
        return StandardProduct(**product_data)

    raw = product_data.get("raw") if isinstance(product_data, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    # Fallback mapping for minimal Shopify-style payload
    shopify_id = product_data.get("shopify_id") or product_data.get("id") or raw.get("id")
    title = product_data.get("title") or raw.get("title") or ""
    variants_count = int(product_data.get("variants_count") or 0 or len(raw.get("variants") or []))
    images_count = int(product_data.get("images_count") or 0 or len(raw.get("images") or []))
    vendor = product_data.get("vendor") or raw.get("vendor")
    product_type = product_data.get("product_type") or raw.get("product_type")
    status_raw = (product_data.get("status") or raw.get("status") or ProductStatus.ACTIVE.value) or ""

    # Derive core sellable fields from raw variants/images when available
    variants: List[StandardProductVariant] = []
    price = 0.0
    inventory_quantity = 0
    sku = None
    barcode = None
    currency = raw.get("presentment_currency") or "USD"

    raw_variants = raw.get("variants") or []
    if isinstance(raw_variants, list) and raw_variants:
        min_positive_price: Optional[float] = None
        for idx, v in enumerate(raw_variants):
            if not isinstance(v, dict):
                continue
            try:
                v_price = float(v.get("price") or 0)
                if v_price > 0 and (min_positive_price is None or v_price < min_positive_price):
                    min_positive_price = v_price
                variant = StandardProductVariant(
                    id=str(v.get("id") or ""),
                    title=v.get("title", "Default"),
                    sku=v.get("sku"),
                    barcode=v.get("barcode"),
                    price=v_price,
                    compare_at_price=float(v["compare_at_price"]) if v.get("compare_at_price") else None,
                    inventory_quantity=int(v.get("inventory_quantity") or 0),
                    weight=v.get("weight"),
                    weight_unit=v.get("weight_unit"),
                    options=None,
                    image_url=None,
                )
                variants.append(variant)
                inventory_quantity += max(int(variant.inventory_quantity or 0), 0)
                if idx == 0:
                    sku = variant.sku
                    barcode = variant.barcode
                    currency = v.get("currency") or raw.get("presentment_currency") or currency
            except Exception:
                continue
        if min_positive_price is not None:
            price = float(min_positive_price)

    image_url = None
    images: List[str] = []
    try:
        imgs = raw.get("images") or []
        if isinstance(imgs, list):
            images = [str(i.get("src")) for i in imgs if isinstance(i, dict) and i.get("src")]
            image_url = images[0] if images else None
    except Exception:
        images = []
        image_url = None

    if not image_url:
        try:
            image_url = (raw.get("image") or {}).get("src") or None
            if image_url:
                images = images or [image_url]
        except Exception:
            pass

    tags: List[str] = []
    raw_tags = raw.get("tags")
    if isinstance(raw_tags, str) and raw_tags.strip():
        tags = [t.strip() for t in raw_tags.replace(";", ",").split(",") if t.strip()]

    published_at = _parse_dt(raw.get("published_at"))
    created_at = _parse_dt(raw.get("created_at")) or _parse_dt(product_data.get("created_at"))
    updated_at = _parse_dt(raw.get("updated_at")) or _parse_dt(product_data.get("updated_at"))

    status = ProductStatus.ACTIVE
    if str(status_raw).lower() == "draft":
        status = ProductStatus.DRAFT
    elif str(status_raw).lower() == "archived":
        status = ProductStatus.ARCHIVED

    # Simple data completeness score (EPIC-4 baseline)
    score = 0.0
    if title:
        score += 0.4
    if images_count > 0 or image_url:
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
        "description": raw.get("body_html") or None,
        "vendor": vendor,
        "product_type": product_type,
        "tags": tags,
        "price": float(price or 0),
        "compare_at_price": None,
        "currency": currency or "USD",
        "inventory_quantity": int(inventory_quantity or 0),
        "sku": sku,
        "barcode": barcode,
        "image_url": image_url,
        "images": images,
        "variants": variants,
        "status": status,
        "published_at": published_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "platform_metadata": {
            "shopify_id": raw.get("id"),
            "handle": raw.get("handle"),
        },
        "data_completeness_score": score,
    }

    return StandardProduct(**mapped)


@router.get("/{merchant_id}", response_model=ProductListResponse)
async def get_merchant_products_v2(
    merchant_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    platform: str = Query(None, description="Filter by platform"),
    current_user: dict = Depends(get_current_user),
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
        logger.info(
            f"[V2] Fetching products for merchant {merchant_id}, "
            f"user_role={current_user.get('role')}, user_merchant_id={current_user.get('merchant_id')}"
        )

        if not can_access_merchant(current_user, merchant_id):
            logger.warning(f"[V2] Access denied for user {current_user.get('email')}")
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Not authorized. Your merchant_id: {current_user.get('merchant_id')}, "
                    f"Requested: {merchant_id}"
                ),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[V2] Error in access check: {e}")
        raise HTTPException(status_code=500, detail=f"Access check failed: {str(e)}")

    try:
        logger.info(f"Fetching products for merchant {merchant_id}, platform={platform}")

        query_conditions = ["merchant_id = :merchant_id"]
        query_params: Dict[str, Any] = {"merchant_id": merchant_id}

        if platform:
            query_conditions.append("platform = :platform")
            query_params["platform"] = platform

        query_conditions.append("(expires_at IS NULL OR expires_at > NOW())")
        where_clause = " AND ".join(query_conditions)

        count_query = f"""
            SELECT COUNT(*) as total
            FROM products_cache
            WHERE {where_clause}
        """
        count_result = await database.fetch_one(count_query, query_params)
        total = count_result["total"] if count_result else 0

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

        products: List[StandardProduct] = []
        for row in rows:
            try:
                product_data = row["product_data"]
                if isinstance(product_data, str):
                    product_data = json.loads(product_data)

                if "platform" not in product_data:
                    product_data["platform"] = row["platform"]

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

        response_platform = "all"
        if products:
            response_platform = products[0].platform
        elif platform:
            response_platform = platform

        return ProductListResponse(
            merchant_id=merchant_id,
            platform=response_platform,
            products=products,
            total=total,
            next_page_token=str(offset + limit) if total > (offset + limit) else None,
            fetched_at=datetime.now(),
        )

    except Exception as e:
        import traceback

        logger.error(f"[V2] Error fetching products: {e}")
        logger.error(f"[V2] Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch products: {str(e)}",
        )


@router.get("/{merchant_id}/platforms")
async def get_merchant_platforms(
    merchant_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get all platforms that have cached products for this merchant"""
    if not can_access_merchant(current_user, merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized to access this merchant's data")

    logger.info(f"[V2] Getting platforms for merchant {merchant_id}")

    platforms_query = """
        SELECT platform,
               COUNT(*) as product_count,
               MAX(cached_at) as last_cached_at
        FROM products_cache
        WHERE merchant_id = :merchant_id
          AND (expires_at IS NULL OR expires_at > NOW())
        GROUP BY platform
        ORDER BY product_count DESC
    """

    rows = await database.fetch_all(platforms_query, {"merchant_id": merchant_id})
    platforms = []
    for row in rows:
        platforms.append(
            {
                "platform": row["platform"],
                "product_count": row["product_count"],
                "last_cached_at": row["last_cached_at"].isoformat() if row["last_cached_at"] else None,
            }
        )

    return {
        "status": "success",
        "merchant_id": merchant_id,
        "platforms": platforms,
    }
