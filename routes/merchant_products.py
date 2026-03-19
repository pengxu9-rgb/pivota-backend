"""
Merchant product optimization APIs.

These endpoints expose a merged view of:
- StandardProduct from products_cache
- Pivota-specific enrichment from product_enrichment
- Latest quality snapshot from product_quality_snapshot

They are intended to power the Merchant Portal "Product Optimization"
experience, not the public Agent API.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any, Dict, List, Optional
from datetime import datetime

from pydantic import BaseModel

from db.database import database
from db.products import products_cache, get_cached_products
from db.product_enrichment import get_enrichment, upsert_enrichment
from db.product_quality import product_quality_snapshot
from models.standard_product import StandardProduct
from services.product_enrichment_pipeline import run_enrichment_for_product
from utils.auth import get_current_user
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/merchant/products", tags=["Merchant Products"])


class EnrichmentBackfillRequest(BaseModel):
    platform: Optional[str] = None
    limit: Optional[int] = 100


async def _fetch_latest_quality_row(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
) -> Optional[Dict[str, Any]]:
    query = """
    SELECT *
    FROM product_quality_snapshot
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id = :platform_product_id
    ORDER BY snapshot_date DESC
    LIMIT 1
    """
    row = await database.fetch_one(
        query,
        {
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
        },
    )
    return dict(row) if row else None


def _build_standard_summary(cache_row: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a lightweight standard view from products_cache row."""
    product_json = cache_row.get("product_data") or {}
    status = None
    orderable = None
    try:
        # Validate via StandardProduct for safety, but don't raise on errors
        product = StandardProduct.parse_obj(product_json)
        title = product.title
        price = {
            "value": product.price,
            "currency": product.currency,
        }
        main_image_url = product.image_url or (product.images[0] if product.images else None)
        status = getattr(product, "status", None)
        orderable = getattr(product, "orderable", None)
    except Exception:
        title = product_json.get("title")
        price_value = product_json.get("price")
        price_currency = product_json.get("currency") or "USD"
        price = {"value": price_value, "currency": price_currency}
        main_image_url = product_json.get("image_url")
        status = product_json.get("status")
        orderable = product_json.get("orderable")

    return {
        "title": title,
        "price": price,
        "main_image_url": main_image_url,
        "status": status,
        "orderable": orderable,
        "last_synced_at": cache_row.get("cached_at"),
    }


@router.get("")
async def list_merchant_products(
    platform: Optional[str] = Query(None),
    include_expired: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """
    List products for the current merchant with optional enrichment & quality.

    This is a lightweight list view for the Merchant Portal.
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can list their products")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    offset = (page - 1) * page_size

    # Base query from products_cache (default: only active rows)
    filters = [products_cache.c.merchant_id == merchant_id]
    if platform:
        filters.append(products_cache.c.platform == platform)
    if not include_expired:
        filters.append(
            or_(
                products_cache.c.expires_at.is_(None),
                products_cache.c.expires_at > datetime.now(),
            )
        )

    base_query = products_cache.select().where(*filters)
    count_query = select(func.count().label("total")).select_from(products_cache).where(*filters)
    count_row = await database.fetch_one(count_query)
    total = 0
    if count_row is not None:
        if isinstance(count_row, dict):
            total = int(count_row.get("total") or 0)
        else:
            total = int(dict(count_row).get("total") or 0)

    query = (
        base_query.order_by(products_cache.c.cached_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    rows = await database.fetch_all(query)

    items: List[Dict[str, Any]] = []
    for row in rows:
        cache_row = dict(row)
        platform_val = cache_row.get("platform")
        platform_product_id = cache_row.get("platform_product_id")

        standard = _build_standard_summary(cache_row)

        enrichment = await get_enrichment(
            merchant_id=merchant_id,
            platform=platform_val,
            platform_product_id=platform_product_id,
            geo_code="default",
        )
        quality = await _fetch_latest_quality_row(
            merchant_id=merchant_id,
            platform=platform_val,
            platform_product_id=platform_product_id,
        )

        items.append({
            "merchant_id": merchant_id,
            "platform": platform_val,
            "platform_product_id": platform_product_id,
            "standard": standard,
            "enrichment": enrichment or {},
            "quality": {
                "content_quality_score": quality.get("content_quality_score") if quality else None,
                "model_readiness_score": quality.get("model_readiness_score") if quality else None,
                "conversion_potential_score": quality.get("conversion_potential_score") if quality else None,
                "last_evaluated_at": quality.get("snapshot_date") if quality else None,
            },
        })

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/quality/summary")
async def get_product_quality_summary(
    current_user: dict = Depends(get_current_user),
):
    """
    Lightweight catalog quality summary for the current merchant.

    Returns:
    - total_products: products in cache
    - scored_products: products that have at least one quality snapshot
    - avg_content_quality
    - avg_model_readiness
    - low_cq_count: products with CQ below threshold
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can view quality summary")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    # Load all cached products (current merchant, all platforms, non-expired)
    base_query = products_cache.select().where(products_cache.c.merchant_id == merchant_id)
    base_query = base_query.where(products_cache.c.expires_at > datetime.now())
    records = await database.fetch_all(base_query)
    rows = [dict(r) for r in records]

    total_products = len(rows)
    scored_products = 0
    sum_cq = 0.0
    sum_mr = 0.0
    low_cq_count = 0
    low_cq_threshold = 60.0

    for row in rows:
        platform_val = row.get("platform")
        platform_product_id = row.get("platform_product_id")
        if not platform_val or not platform_product_id:
            continue

        quality = await _fetch_latest_quality_row(
            merchant_id=merchant_id,
            platform=platform_val,
            platform_product_id=platform_product_id,
        )
        if not quality:
            continue

        cq = quality.get("content_quality_score")
        mr = quality.get("model_readiness_score")
        if cq is None and mr is None:
            continue

        scored_products += 1
        if isinstance(cq, (int, float)):
            sum_cq += float(cq)
            if cq < low_cq_threshold:
                low_cq_count += 1
        if isinstance(mr, (int, float)):
            sum_mr += float(mr)

    avg_cq = sum_cq / scored_products if scored_products and sum_cq > 0 else None
    avg_mr = sum_mr / scored_products if scored_products and sum_mr > 0 else None

    return {
        "status": "success",
        "data": {
            "total_products": total_products,
            "scored_products": scored_products,
            "avg_content_quality": avg_cq,
            "avg_model_readiness": avg_mr,
            "low_cq_threshold": low_cq_threshold,
            "low_cq_count": low_cq_count,
        },
    }


@router.get("/{platform}/{platform_product_id}")
async def get_merchant_product_detail(
    platform: str,
    platform_product_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Detailed view for a single product:
    - StandardProduct (from products_cache)
    - Enrichment overlay
    - Latest quality snapshot
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can view their products")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    cache_row = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not cache_row:
        raise HTTPException(status_code=404, detail="Product not found in cache")

    cache_row = dict(cache_row)
    product_json = cache_row.get("product_data") or {}
    standard_full: Dict[str, Any]
    try:
        product = StandardProduct.parse_obj(product_json)
        standard_full = product.dict()
    except Exception:
        standard_full = product_json

    enrichment = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    )
    quality = await _fetch_latest_quality_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "standard": standard_full,
        "enrichment": enrichment or {},
        "quality": quality or {},
    }


@router.post("/enrichment/backfill")
async def backfill_product_enrichment(
    body: EnrichmentBackfillRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Run enrichment + quality scoring for multiple products of the current merchant.

    Body:
    - platform: optional platform filter (e.g. "wix", "shopify")
    - limit: optional max number of products to process (default 100)
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can optimize products")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    platform = body.platform
    limit = body.limit or 100
    if limit <= 0:
        limit = 1
    if limit > 1000:
        limit = 1000

    rows: List[Dict[str, Any]] = []

    # If platform specified, use helper; otherwise query all platforms for this merchant.
    if platform:
        cached = await get_cached_products(
            merchant_id=merchant_id,
            platform=platform,
            include_expired=False,
        )
        rows = cached[:limit]
    else:
        base_query = products_cache.select().where(products_cache.c.merchant_id == merchant_id)
        base_query = base_query.where(products_cache.c.expires_at > datetime.now())
        base_query = base_query.order_by(products_cache.c.cached_at.desc()).limit(limit)
        records = await database.fetch_all(base_query)
        rows = [dict(r) for r in records]

    processed = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    for row in rows:
        platform_val = row.get("platform")
        platform_product_id = row.get("platform_product_id")
        if not platform_product_id or not platform_val:
            skipped += 1
            continue
        try:
            await run_enrichment_for_product(
                merchant_id=merchant_id,
                platform=platform_val,
                platform_product_id=platform_product_id,
                geo_code="default",
            )
            processed += 1
        except Exception as exc:
            skipped += 1
            errors.append(
                {
                    "platform": platform_val,
                    "platform_product_id": platform_product_id,
                    "error": str(exc),
                }
            )

    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "platform": platform,
            "limit": limit,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
        },
    }


@router.put("/{platform}/{platform_product_id}/enrichment")
async def update_product_enrichment(
    platform: str,
    platform_product_id: str,
    body: Dict[str, Any],
    current_user: dict = Depends(get_current_user),
):
    """
    Update enrichment overlay for a product.

    The body may contain any subset of enrichment fields:
    - title_override, summary_short, bullet_points, usage_scenarios,
      audience_tags, topic_tags, regulatory_disclaimer_local, extra_images
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can modify enrichment")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    # Ensure the product exists for this merchant
    exists = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Product not found")

    await upsert_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
        data=body,
    )

    enrichment = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    )

    return {
        "status": "success",
        "enrichment": enrichment or {},
    }


@router.post("/{platform}/{platform_product_id}/enrichment/run")
async def run_product_enrichment(
    platform: str,
    platform_product_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Trigger the automated enrichment pipeline for a single product,
    then return the updated combined detail view.
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can optimize products")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    # Ensure the product exists for this merchant
    exists = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Product not found in cache")

    # Run enrichment pipeline (heuristic AI + quality snapshot + event)
    try:
        await run_enrichment_for_product(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            geo_code="default",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run enrichment pipeline: {exc}",
        )

    # Build updated detail view (same shape as GET /merchant/products/{platform}/{platform_product_id})
    cache_row = dict(exists)
    product_json = cache_row.get("product_data") or {}
    try:
        product = StandardProduct.parse_obj(product_json)
        standard_full: Dict[str, Any] = product.dict()
    except Exception:
        standard_full = product_json

    enrichment = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    )
    quality = await _fetch_latest_quality_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "standard": standard_full,
        "enrichment": enrichment or {},
        "quality": quality or {},
    }
