"""
Merchant product optimization APIs.

These endpoints expose a merged view of:
- StandardProduct from products_cache
- Pivota-specific enrichment from product_enrichment
- Latest quality snapshot from product_quality_snapshot

They are intended to power the Merchant Portal "Product Optimization"
experience, not the public Agent API.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from pydantic import BaseModel

from db.database import database
from db.products import products_cache, get_cached_products
from db.product_enrichment import (
    get_enrichment,
    get_enrichments_for_products,
    upsert_enrichment,
)
from db.product_quality_backfill_jobs import (
    create_quality_backfill_job,
    get_active_quality_backfill_job,
    get_quality_backfill_job,
)
from models.standard_product import StandardProduct
from services.product_enrichment_pipeline import run_enrichment_for_product
from services.product_exposure_service import build_agent_push_projection_from_cache_row
from services.product_quality_backfill_service import process_quality_backfill_job
from services.product_quality_service import (
    build_quality_payload_from_cache_row,
    build_quality_projection,
    fetch_latest_quality_rows,
    make_product_key,
    summarize_quality_coverage,
)
from utils.auth import get_current_user
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/merchant/products", tags=["Merchant Products"])


class EnrichmentBackfillRequest(BaseModel):
    platform: Optional[str] = None
    limit: Optional[int] = 100


class QualityBackfillRequest(BaseModel):
    platform: Optional[str] = None
    force_refresh: bool = False
    missing_only: bool = True


def _quality_response(projection: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content_quality_score": projection.get("content_quality_score"),
        "model_readiness_score": projection.get("model_readiness_score"),
        "conversion_potential_score": projection.get("conversion_potential_score"),
        "last_evaluated_at": projection.get("last_evaluated_at"),
        "quality_source": projection.get("quality_source", "none"),
    }


def _agent_push_response(projection: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent_push_status": projection.get("agent_push_status", "eligible_for_agent_push"),
        "agent_push_reason_codes": projection.get("agent_push_reason_codes", []),
        "eligible_variant_count": projection.get("eligible_variant_count", 0),
        "excluded_variant_count": projection.get("excluded_variant_count", 0),
        "store_data_last_checked_at": projection.get("store_data_last_checked_at"),
    }


async def _build_quality_projection_bundle(
    merchant_id: str,
    cache_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    product_keys: List[Tuple[str, str]] = []
    for row in cache_rows:
        key = make_product_key(row.get("platform"), row.get("platform_product_id"))
        if key is not None:
            product_keys.append(key)

    if not product_keys:
        return {
            "enrichments_by_key": {},
            "snapshot_rows_by_key": {},
            "projections_by_key": {},
            "coverage": summarize_quality_coverage(
                [],
                projections_by_key={},
                snapshot_rows_by_key={},
                active_backfill_job=None,
            ),
        }

    enrichments_by_key = await get_enrichments_for_products(
        merchant_id,
        product_keys=product_keys,
        geo_code="default",
    )
    snapshot_rows_by_key = await fetch_latest_quality_rows(
        merchant_id,
        platforms=sorted({platform for platform, _ in product_keys}),
        product_keys=product_keys,
    )
    active_backfill_job = await get_active_quality_backfill_job(merchant_id)

    projections_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in cache_rows:
        key = make_product_key(row.get("platform"), row.get("platform_product_id"))
        if key is None:
            continue
        payload = build_quality_payload_from_cache_row(row, enrichments_by_key.get(key) or {})
        projections_by_key[key] = build_quality_projection(
            snapshot_row=snapshot_rows_by_key.get(key),
            payload=payload,
        )

    return {
        "enrichments_by_key": enrichments_by_key,
        "snapshot_rows_by_key": snapshot_rows_by_key,
        "projections_by_key": projections_by_key,
        "coverage": summarize_quality_coverage(
            product_keys,
            projections_by_key=projections_by_key,
            snapshot_rows_by_key=snapshot_rows_by_key,
            active_backfill_job=active_backfill_job,
        ),
    }


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
    cache_rows = [dict(row) for row in rows]
    quality_bundle = await _build_quality_projection_bundle(merchant_id, cache_rows)

    items: List[Dict[str, Any]] = []
    for cache_row in cache_rows:
        platform_val = cache_row.get("platform")
        platform_product_id = cache_row.get("platform_product_id")
        product_key = make_product_key(platform_val, platform_product_id)

        standard = _build_standard_summary(cache_row)
        enrichment = quality_bundle["enrichments_by_key"].get(product_key or ("", ""), {})
        projection = quality_bundle["projections_by_key"].get(product_key or ("", ""), {})
        agent_push = build_agent_push_projection_from_cache_row(cache_row)

        items.append({
            "merchant_id": merchant_id,
            "platform": platform_val,
            "platform_product_id": platform_product_id,
            "standard": standard,
            "enrichment": enrichment or {},
            "quality": _quality_response(projection),
            "agent_push": _agent_push_response(agent_push),
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
    quality_bundle = await _build_quality_projection_bundle(merchant_id, rows)
    coverage = quality_bundle["coverage"]
    sum_cq = 0.0
    sum_mr = 0.0
    cq_count = 0
    mr_count = 0
    low_cq_count = 0
    low_cq_threshold = 60.0

    for row in rows:
        key = make_product_key(row.get("platform"), row.get("platform_product_id"))
        projection = quality_bundle["projections_by_key"].get(key or ("", ""), {})
        cq = projection.get("content_quality_score")
        mr = projection.get("model_readiness_score")
        if cq is None and mr is None:
            continue

        if isinstance(cq, (int, float)):
            sum_cq += float(cq)
            cq_count += 1
            if cq < low_cq_threshold:
                low_cq_count += 1
        if isinstance(mr, (int, float)):
            sum_mr += float(mr)
            mr_count += 1

    avg_cq = sum_cq / cq_count if cq_count > 0 else None
    avg_mr = sum_mr / mr_count if mr_count > 0 else None

    return {
        "status": "success",
        "data": {
            "total_products": total_products,
            "scored_products": coverage["effective_scored_products"],
            "avg_content_quality": avg_cq,
            "avg_model_readiness": avg_mr,
            "low_cq_threshold": low_cq_threshold,
            "low_cq_count": low_cq_count,
            "snapshot_scored_products": coverage["snapshot_scored_products"],
            "effective_scored_products": coverage["effective_scored_products"],
            "preview_only_products": coverage["preview_only_products"],
            "unscored_products": coverage["unscored_products"],
            "coverage_state": coverage["coverage_state"],
            "latest_snapshot_at": coverage["latest_snapshot_at"],
            "backfill_recommended": coverage["backfill_recommended"],
            "active_backfill_job": coverage["active_backfill_job"],
        },
    }


@router.post("/quality/backfill", status_code=202)
async def queue_quality_backfill(
    body: QualityBackfillRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can backfill quality scores")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    requested_by = (
        current_user.get("email")
        or current_user.get("user_id")
        or current_user.get("merchant_id")
    )

    active_job = await get_active_quality_backfill_job(
        merchant_id,
        platform=body.platform,
    )
    if active_job is not None:
        return {
            "status": "queued",
            "data": {
                "job": active_job,
                "already_active": True,
            },
        }

    job = await create_quality_backfill_job(
        merchant_id=merchant_id,
        platform=body.platform,
        requested_by=str(requested_by) if requested_by else None,
        force_refresh=body.force_refresh,
        missing_only=body.missing_only,
    )
    background_tasks.add_task(process_quality_backfill_job, job.get("job_id"))

    return {
        "status": "queued",
        "data": {
            "job": job,
            "already_active": False,
        },
    }


@router.get("/quality/backfill/{job_id}")
async def get_quality_backfill_status(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can view quality backfill jobs")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    job = await get_quality_backfill_job(job_id)
    if job is None or job.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=404, detail="Quality backfill job not found")

    return {
        "status": "success",
        "data": job,
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
    quality_bundle = await _build_quality_projection_bundle(merchant_id, [cache_row])
    projection = quality_bundle["projections_by_key"].get(
        make_product_key(platform, platform_product_id) or ("", ""),
        {},
    )
    agent_push = build_agent_push_projection_from_cache_row(cache_row)

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "standard": standard_full,
        "enrichment": enrichment or {},
        "quality": _quality_response(projection),
        "agent_push": _agent_push_response(agent_push),
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
    quality_bundle = await _build_quality_projection_bundle(merchant_id, [cache_row])
    projection = quality_bundle["projections_by_key"].get(
        make_product_key(platform, platform_product_id) or ("", ""),
        {},
    )

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "standard": standard_full,
        "enrichment": enrichment or {},
        "quality": _quality_response(projection),
    }
