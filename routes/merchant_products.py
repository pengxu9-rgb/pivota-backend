"""
Merchant product optimization APIs.

These endpoints expose a merged view of:
- StandardProduct from products_cache
- Pivota-specific enrichment from product_enrichment
- Latest quality snapshot from product_quality_snapshot

They are intended to power the Merchant Portal "Product Optimization"
experience, not the public Agent API.
"""

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime, timezone

from pydantic import BaseModel

from db.canonical_commerce import canonical_products
from db.database import database
from db.products import products_cache, get_cached_products
from db.product_enrichment import (
    get_enrichment,
    get_enrichments_for_products,
    upsert_enrichment,
)
from db.product_quality_backfill_jobs import (
    complete_quality_backfill_job,
    create_quality_backfill_job,
    get_active_quality_backfill_job,
    get_quality_backfill_job,
)
from models.standard_product import StandardProduct
from services.canonical_commerce_service import (
    load_canonical_cache_row,
    load_canonical_cache_rows,
)
from services.beauty_field_authoring import (
    ALLOWED_SKIN_CONCERNS,
    SUBCATEGORY_GROUP_BEAUTY_CARE,
    SUBCATEGORY_GROUP_BEAUTY_TOOLS,
    SUBCATEGORY_GROUPS as BEAUTY_SUBCATEGORY_GROUPS,
    SUBCATEGORY_SCHEMAS as BEAUTY_SUBCATEGORY_SCHEMAS,
    subcategory_for_path as beauty_subcategory_for_path,
    write_merchant_authored_beauty_fields,
)
from services.fashion_field_authoring import (
    write_merchant_authored_fashion_fields,
)
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
from utils.rich_text import rich_text_to_plain_text
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/merchant/products", tags=["Merchant Products"])

QUALITY_BACKFILL_STALE_SECONDS = 15 * 60


class EnrichmentBackfillRequest(BaseModel):
    platform: Optional[str] = None
    limit: Optional[int] = 100


class QualityBackfillRequest(BaseModel):
    platform: Optional[str] = None
    force_refresh: bool = False
    missing_only: bool = True


def _parse_quality_backfill_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None
    return None


def _quality_backfill_job_is_stale(job: Dict[str, Any]) -> bool:
    if str(job.get("status") or "").lower() != "running":
        return False
    started_at = _parse_quality_backfill_datetime(job.get("started_at"))
    if not started_at:
        return False
    return (datetime.utcnow() - started_at).total_seconds() > QUALITY_BACKFILL_STALE_SECONDS


async def _process_quality_backfill_job_safely(job_id: Optional[str]) -> None:
    if not job_id:
        return
    try:
        await process_quality_backfill_job(str(job_id))
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "quality_backfill_background_failed",
            extra={"job_id": str(job_id), "error": str(exc)[:240]},
        )


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
    description = None
    description_text = None
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
        description = getattr(product, "description", None)
        description_text = getattr(product, "description_text", None)
    except Exception:
        title = product_json.get("title")
        price_value = product_json.get("price")
        price_currency = product_json.get("currency") or "USD"
        price = {"value": price_value, "currency": price_currency}
        main_image_url = product_json.get("image_url")
        status = product_json.get("status")
        orderable = product_json.get("orderable")
        description = (
            product_json.get("description")
            or product_json.get("body_html")
            or None
        )
        description_text = rich_text_to_plain_text(
            product_json.get("description_text")
            or description
            or ""
        ) or None

    return {
        "title": title,
        "description": description,
        "description_text": description_text,
        "price": price,
        "main_image_url": main_image_url,
        "status": status,
        "orderable": orderable,
        "last_synced_at": cache_row.get("cached_at"),
    }


def _build_standard_full(product_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        product = StandardProduct.parse_obj(product_json)
        return product.dict()
    except Exception:
        standard_full = dict(product_json or {})
        standard_full["description_text"] = rich_text_to_plain_text(
            standard_full.get("description_text")
            or standard_full.get("description")
            or standard_full.get("body_html")
            or ""
        ) or None
        return standard_full


async def _load_runtime_product_rows(
    *,
    merchant_id: str,
    platform: Optional[str],
    include_expired: bool,
    page_size: int,
    offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    canonical_rows = await load_canonical_cache_rows(
        merchant_id=merchant_id,
        platform=platform,
        include_expired=include_expired,
        limit=page_size,
        offset=offset,
    )
    if canonical_rows:
        filters = [canonical_products.c.merchant_id == merchant_id]
        if platform:
            filters.append(canonical_products.c.platform == platform)
        if not include_expired:
            filters.append(
                or_(
                    canonical_products.c.expires_at.is_(None),
                    canonical_products.c.expires_at > datetime.now(),
                )
            )
        count_row = await database.fetch_one(
            select(func.count().label("total")).select_from(canonical_products).where(*filters)
        )
        total = 0
        if count_row is not None:
            total = int((dict(count_row) if not isinstance(count_row, dict) else count_row).get("total") or 0)
        return canonical_rows, total

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
        total = int((dict(count_row) if not isinstance(count_row, dict) else count_row).get("total") or 0)

    query = (
        base_query.order_by(products_cache.c.cached_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    rows = await database.fetch_all(query)
    return [dict(row) for row in rows], total


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

    cache_rows, total = await _load_runtime_product_rows(
        merchant_id=merchant_id,
        platform=platform,
        include_expired=include_expired,
        page_size=page_size,
        offset=offset,
    )
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

    rows = await load_canonical_cache_rows(
        merchant_id=merchant_id,
        platform=None,
        include_expired=False,
    )
    if not rows:
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
        if _quality_backfill_job_is_stale(active_job):
            await complete_quality_backfill_job(
                str(active_job.get("job_id")),
                status="failed",
                total_candidates=active_job.get("total_candidates"),
                processed=active_job.get("processed"),
                skipped=active_job.get("skipped"),
                failed=active_job.get("failed"),
                errors_sample=[
                    {
                        "error": "stale_quality_backfill_job_replaced",
                        "message": "Stale running quality backfill was marked failed before queueing a replacement.",
                    }
                ],
            )
        else:
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
    background_tasks.add_task(_process_quality_backfill_job_safely, job.get("job_id"))

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

    cache_row = await load_canonical_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    if not cache_row:
        db_row = await database.fetch_one(
            products_cache.select().where(
                (products_cache.c.merchant_id == merchant_id)
                & (products_cache.c.platform == platform)
                & (products_cache.c.platform_product_id == platform_product_id)
            )
        )
        cache_row = dict(db_row) if db_row else None

    if not cache_row:
        raise HTTPException(status_code=404, detail="Product not found")

    product_json = cache_row.get("product_data") or {}
    standard_full = _build_standard_full(product_json)

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

    # Store-admin deep-link for the copy-back rung (read-only): lets the merchant
    # open THIS product in their own store admin and paste the enriched copy. The
    # copy itself is already returned in `enrichment`; this just supplies the
    # paste-target link. Best-effort — None when no matching active store / a
    # platform without an admin URL. Pure read; no external write. Reuses the
    # readiness builder (lazy import avoids a circular import + matches the
    # codebase's lazy-import convention for cross-module helpers).
    platform_admin_url: Optional[str] = None
    try:
        from readiness.summary import (
            _build_platform_admin_url,
            _load_store_domains_by_platform,
        )

        store_domains = await _load_store_domains_by_platform(merchant_id)
        platform_admin_url = _build_platform_admin_url(
            platform=platform,
            platform_product_id=platform_product_id,
            store_domains_by_platform=store_domains,
        )
    except Exception as exc:  # noqa: BLE001 -- the link is a nicety; never fail the detail view
        import logging

        logging.getLogger(__name__).warning(
            "merchant_products: platform_admin_url build failed for %s/%s: %s",
            platform, platform_product_id, str(exc)[:200],
        )

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "standard": standard_full,
        "enrichment": enrichment or {},
        "quality": _quality_response(projection),
        "agent_push": _agent_push_response(agent_push),
        "platform_admin_url": platform_admin_url,
    }


@router.post("/{platform}/{platform_product_id}/store_pdp/publish")
async def publish_store_pdp(
    platform: str,
    platform_product_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Publish the AI-ready enrichment copy to the merchant's OWN store PDP as an
    APP-OWNED Shopify metafield (pivota/ai_pdp) — NEVER body_html. The merchant
    surfaces it in their theme; nothing customer-visible changes until they do.

    First write to a merchant's live store, so it is fail-closed and GATED:
    default-OFF per-store content_writeback state machine + the
    DISABLE_CONTENT_WRITEBACK global kill switch + (at the Shopify layer) the
    write_products scope on the merchant's own connected (BYO / custom) app token.
    Returns a {status} envelope (200) the caller maps to a UI state — `blocked`
    when not opted in, `needs_write_products` when the connected app's token lacks
    the scope (the merchant enables it on their app), `written` on success.
    Explicit merchant action only; never auto-dispatched."""
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can publish store PDP copy")
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    enrichment = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    )
    if not enrichment:
        return {
            "status": "no_copy",
            "message": "No enrichment copy to publish for this product yet.",
        }

    from services.shopify_content_writeback import publish_content_to_store

    return await publish_content_to_store(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        enrichment=enrichment,
    )


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
        canonical_rows = await load_canonical_cache_rows(
            merchant_id=merchant_id,
            platform=platform,
            include_expired=False,
            limit=limit,
        )
        if canonical_rows:
            rows = canonical_rows[:limit]
        else:
            cached = await get_cached_products(
                merchant_id=merchant_id,
                platform=platform,
                include_expired=False,
            )
            rows = cached[:limit]
    else:
        canonical_rows = await load_canonical_cache_rows(
            merchant_id=merchant_id,
            platform=None,
            include_expired=False,
            limit=limit,
        )
        if canonical_rows:
            rows = canonical_rows
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


class FashionFieldsBody(BaseModel):
    """Merchant-authored fashion fields for one product.

    null on a field means "leave unchanged". Explicit-clear is a v2
    concern; today there's no way to null out a previously-authored
    value via this endpoint.

    size_guide accepts a plain string (wrapped server-side into
    {raw: ...} for the JSONB column) or a structured chart dict.
    Lists / numbers / bools are rejected — the column gates
    merchant-facing prose and untyped input shouldn't reach it.
    """
    material: Optional[str] = None
    care: Optional[str] = None
    size_guide: Optional[Union[str, Dict[str, Any]]] = None


@router.put("/{platform}/{platform_product_id}/fashion_fields")
async def update_product_fashion_fields(
    platform: str,
    platform_product_id: str,
    body: FashionFieldsBody,
    current_user: dict = Depends(get_current_user),
):
    """Merchant-authored fashion-field write path (material / care / size_guide).

    Writes directly to catalog_products with source=merchant_authored,
    confidence=1.0, inside a per-row transaction (race-safe vs concurrent
    merchant_payload sync). The write respects existing merchant_payload
    values (Shopify metafields are authoritative — the merchant should
    edit those in their source platform, not here). LLM-extracted values
    are overwritten. Triggers an inline agent_pdp_view refresh on any
    successful write so the gateway sees the change immediately.

    Response codes:
      - 200 with `outcomes` dict — at least one field was meaningfully
        considered. Inspect each field's outcome:
          'written' / 'skipped_payload_owned' / 'unchanged'
      - 404 — no catalog_products row for this (merchant, platform,
        source_product_id) tuple. Distinct from the products_cache
        ownership 404 above so the merchant can tell the difference
        between "you don't own this product" and "this product hasn't
        been ingested into the canonical catalog yet."
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(
            status_code=403,
            detail="Only merchants can author fashion fields",
        )

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    # Ownership check — same shape as the /enrichment endpoint above.
    exists = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Product not found")

    outcomes = await write_merchant_authored_fashion_fields(
        merchant_id=merchant_id,
        platform=platform,
        source_product_id=platform_product_id,
        material=body.material,
        care=body.care,
        size_guide=body.size_guide,
    )

    # If every reported outcome is product_not_found, surface as HTTP 404
    # rather than burying the failure under top-level 200/"success".
    # Codex review flagged the "false success" risk; this fixes it.
    if outcomes and all(v == "product_not_found" for v in outcomes.values()):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "catalog_product_not_found",
                "message": (
                    "Product exists in your platform cache but no canonical "
                    "catalog row has been ingested yet. Re-sync from your "
                    "platform and retry."
                ),
                "outcomes": outcomes,
            },
        )

    return {
        "status": "success",
        "outcomes": outcomes,
    }


# ----------------------------------------------------------------------------
# Fashion-field completeness — read endpoint for the merchant agent surface.
#
# The existing /merchant/readiness/optimization endpoint computes
# fashion-field missing codes at the *lane* level (aggregate), not per
# product, so the agent surface couldn't reliably enumerate "the 19
# products missing material" from that payload alone. This is the
# dedicated read for that flow.
# ----------------------------------------------------------------------------

# Category prefixes the gateway / writer treats as fashion. Mirrors
# services/fashion_field_extractor._FASHION_CATEGORY_PREFIXES. Keep in
# sync — drift would silently exclude products from the agent surface.
_FASHION_CATEGORY_PREFIXES = ("fashion/", "apparel/", "clothing/", "shoes/", "accessories/")


def _fashion_field_status(value: Any, source: Optional[str]) -> str:
    """Map a (value, source) row to the UI's FieldStatus enum.

    Mirrors types/fashion-authoring.ts on the UI side. Stable strings
    here — adding a new status requires a coordinated UI change.
    """
    if value is None or value == "":
        return "missing"
    if source == "merchant_payload":
        return "merchant-payload-locked"
    if source == "merchant_authored":
        return "merchant-authored"
    if source == "llm_extraction_v1":
        return "filled-by-llm"
    if source == "external_seed":
        return "inherited"
    # Fallback when value exists but source is unknown (legacy rows).
    return "merchant-authored"


@router.get("/fashion_completeness")
async def get_fashion_completeness(
    page: int = Query(1, ge=1),
    # Max raised from 200 → 500 so brands with 200-500 fashion SKUs can
    # walk the full queue in one fetch. Past 500 the one-at-a-time UX
    # in the agent surface stops being a sensible interaction model and
    # the right answer is the bulk-by-product-type / CSV paths planned
    # for v2 — capping here keeps the v1 surface honest about what it
    # can handle. Server cost is essentially flat across this range
    # (one indexed scan + LIMIT); the binding constraint is response
    # size + client-side localStorage persist, both fine through 500.
    page_size: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """List the merchant's fashion-categorized products with per-field
    material / care / size_guide state.

    Powers the merchant agent surface (`/dashboard/agent-chat`):
      - The trigger surface uses `queue` to render counts + sample chips
      - The structured editor walks `queue` row-by-row
      - The honest-feedback screen reads each product's per-field source

    Filters server-side to products where at least one of material /
    care / size_guide is missing (NULL or empty). Products already
    fully covered don't need the agent to prompt.

    Response shape (stable; the UI's `IncompleteProduct` type maps to it):
        {
          "status": "success",
          "data": {
            "queue": [ { platform, platform_product_id, title, image_url,
                          sku, fields: { material: { status, value,
                          confidence }, care: {...}, size_guide: {...} } } ],
            "totals": { fashion_total, missing_material, missing_care,
                         missing_size_guide, page, page_size,
                         has_more }
          }
        }
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can read fashion completeness")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    category_clause = " OR ".join(
        f"LOWER(cp.category_path) LIKE '{p}%'" for p in _FASHION_CATEGORY_PREFIXES
    )
    offset = (page - 1) * page_size

    # Counts first — cheap, lets the UI render totals before the queue
    # paginates. One round-trip; counts and queue both pull from the
    # same FROM clause via a filtered aggregate.
    totals_sql = f"""
        SELECT
          COUNT(*) AS fashion_total,
          COUNT(*) FILTER (WHERE cp.material IS NULL OR cp.material = '') AS missing_material,
          COUNT(*) FILTER (WHERE cp.care IS NULL OR cp.care = '') AS missing_care,
          COUNT(*) FILTER (WHERE cp.size_guide IS NULL) AS missing_size_guide,
          COUNT(*) FILTER (WHERE
            (cp.material IS NULL OR cp.material = '')
            OR (cp.care IS NULL OR cp.care = '')
            OR cp.size_guide IS NULL
          ) AS total_incomplete
        FROM catalog_products cp
        WHERE cp.merchant_id = :merchant_id
          AND ({category_clause})
    """

    queue_sql = f"""
        SELECT
          cp.platform,
          cp.source_product_id AS platform_product_id,
          cp.title,
          cp.image_url,
          cp.material,
          cp.material_source,
          cp.material_confidence,
          cp.care,
          cp.care_source,
          cp.care_confidence,
          cp.size_guide,
          cp.size_guide_source,
          cp.size_guide_confidence,
          cp.category_path
        FROM catalog_products cp
        WHERE cp.merchant_id = :merchant_id
          AND ({category_clause})
          AND (
            (cp.material IS NULL OR cp.material = '')
            OR (cp.care IS NULL OR cp.care = '')
            OR cp.size_guide IS NULL
          )
        ORDER BY
          -- Stable, deterministic order so paging is consistent across
          -- requests and the cursor in the agent surface lands on the
          -- same product on remount.
          cp.last_seen_in_sync_at DESC NULLS LAST,
          cp.product_key ASC
        LIMIT :limit OFFSET :offset
    """

    totals_row = await database.fetch_one(
        totals_sql, {"merchant_id": merchant_id},
    )
    rows = await database.fetch_all(
        queue_sql,
        {"merchant_id": merchant_id, "limit": page_size, "offset": offset},
    )

    totals = dict(totals_row) if totals_row else {
        "fashion_total": 0, "missing_material": 0,
        "missing_care": 0, "missing_size_guide": 0,
        "total_incomplete": 0,
    }
    queue: List[Dict[str, Any]] = []
    for r in rows or []:
        row = dict(r)
        material_val = row.get("material")
        care_val = row.get("care")
        size_guide_val = row.get("size_guide")
        queue.append({
            "platform": row["platform"],
            "platform_product_id": row["platform_product_id"],
            "title": row.get("title") or "Untitled product",
            "image_url": row.get("image_url"),
            # source_product_id stands in for SKU here — the merchant
            # recognizes their products by platform identifier; a real
            # SKU column on catalog_products isn't always populated.
            "sku": None,
            "fields": {
                "material": {
                    "status": _fashion_field_status(material_val, row.get("material_source")),
                    "value": material_val,
                    "confidence": row.get("material_confidence"),
                },
                "care": {
                    "status": _fashion_field_status(care_val, row.get("care_source")),
                    "value": care_val,
                    "confidence": row.get("care_confidence"),
                },
                "size_guide": {
                    "status": _fashion_field_status(size_guide_val, row.get("size_guide_source")),
                    "value": size_guide_val,
                    "confidence": row.get("size_guide_confidence"),
                },
            },
        })

    total_incomplete = int(totals.get("total_incomplete") or 0)
    has_more = (page * page_size) < total_incomplete

    return {
        "status": "success",
        "data": {
            "queue": queue,
            "totals": {
                "fashion_total": int(totals.get("fashion_total") or 0),
                "missing_material": int(totals.get("missing_material") or 0),
                "missing_care": int(totals.get("missing_care") or 0),
                "missing_size_guide": int(totals.get("missing_size_guide") or 0),
                "total_incomplete": total_incomplete,
                "page": page,
                "page_size": page_size,
                "has_more": has_more,
            },
        },
    }


# ----------------------------------------------------------------------------
# Beauty-field authoring + completeness — parallel of the fashion surface
# at services/beauty_field_authoring.py. Beauty stores authored values
# across three tables (ingredients per-SKU, usage guides per-product,
# concerns per-product) instead of flat columns on catalog_products, so
# the endpoint shape mirrors fashion at the contract layer but the
# underlying writes are multi-table.
# ----------------------------------------------------------------------------


class BeautyFieldsBody(BaseModel):
    """Merchant-authored beauty fields for one product.

    null on a field means "leave unchanged". Different subcategories
    use different field subsets:
      - skincare/haircare/bath/body: raw_inci, how_to_use_text, skin_concerns
      - makeup: raw_inci, how_to_use_text
      - tools: tool_material, use_with, care_instructions

    The body accepts the union; the service writes only the applicable
    fields based on the product's subcategory.
    """
    # Skincare-shape fields
    raw_inci: Optional[str] = None
    how_to_use_text: Optional[str] = None
    skin_concerns: Optional[List[str]] = None
    # Tools-shape fields (v2.1)
    tool_material: Optional[str] = None
    use_with: Optional[str] = None
    care_instructions: Optional[str] = None


@router.put("/{platform}/{platform_product_id}/beauty_fields")
async def update_product_beauty_fields(
    platform: str,
    platform_product_id: str,
    body: BeautyFieldsBody,
    current_user: dict = Depends(get_current_user),
):
    """Merchant-authored beauty-field write path.

    Writes the three primary merchant-input beauty fields across the
    beauty_* tables. Race-safe via per-row transactional locks. Returns
    a per-field outcomes dict mirroring the fashion endpoint so the UI
    can share outcome-handling.

    Auth + ownership match the fashion endpoint exactly.
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(
            status_code=403,
            detail="Only merchants can author beauty fields",
        )

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    exists = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Product not found")

    outcomes = await write_merchant_authored_beauty_fields(
        merchant_id=merchant_id,
        platform=platform,
        source_product_id=platform_product_id,
        raw_inci=body.raw_inci,
        how_to_use_text=body.how_to_use_text,
        skin_concerns=body.skin_concerns,
        tool_material=body.tool_material,
        use_with=body.use_with,
        care_instructions=body.care_instructions,
    )

    if outcomes and all(v == "product_not_found" for v in outcomes.values()):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "catalog_product_not_found",
                "message": (
                    "Product exists in your platform cache but no canonical "
                    "catalog row has been ingested yet. Re-sync from your "
                    "platform and retry."
                ),
                "outcomes": outcomes,
            },
        )

    return {
        "status": "success",
        "outcomes": outcomes,
        # Surface the allowed enum so a thin client doesn't need to
        # hard-code it; the UI can render a multi-select from this.
        "allowed_skin_concerns": list(ALLOWED_SKIN_CONCERNS),
    }


def _beauty_field_status(value: Any) -> str:
    """Beauty fields don't all have *_source columns, so the status
    reduces to "missing" / "merchant-authored" for v1. raw_inci has
    source_system and gets the full enum (including
    merchant-payload-locked) elsewhere."""
    if value is None or value == "":
        return "missing"
    if isinstance(value, list) and not value:
        return "missing"
    return "merchant-authored"


@router.get("/beauty_completeness")
async def get_beauty_completeness(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    # v2.1.1: optional filter. The UI surfaces two distinct beauty tabs
    # — "Beauty care" (skincare/haircare/bath/body/makeup) and
    # "Beauty tools" (brushes/sponges). The merchant picks one; the
    # endpoint scopes to that group's category-path prefixes only.
    # Omitting the param returns both groups (legacy v2.1 behavior).
    subcategory_group: Optional[str] = Query(
        None, pattern="^(beauty_care|beauty_tools)$",
    ),
    current_user: dict = Depends(get_current_user),
):
    """v2.1 subcategory-aware completeness for beauty products.

    For each beauty product in a supported subcategory (per
    BEAUTY_SUBCATEGORY_SCHEMAS — skincare/haircare/bath/body/makeup/tools
    today; fragrance + accessories deferred to v2.2):
      - returns the product's `subcategory_kind` ("skincare" / "tools" /
        etc.) so the UI can pick the right editor form
      - returns `field_schemas`: per-product list of {name, type, label,
        placeholder, hint, allowed_values} so the UI renders generic
        forms without hard-coding the field set
      - returns `fields`: per-product per-field {status, value} only
        for the subcategory's applicable fields

    Filters server-side to products where at least one applicable
    field is missing. Products in unsupported subcategories (fragrance,
    accessories) are excluded entirely until v2.2 ships their schemas.

    The schema-driven approach replaces v2.0's flat "always return INCI
    + how-to-use + concerns" shape. Codex review for the brush case
    (PawStyle had 608 brushes asking for ingredients) is structurally
    fixed: brushes are in subcategory "tools" and get tool_material /
    use_with / care_instructions instead.
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can read beauty completeness")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id on current user")

    # Build the WHERE clause from the v2.1 schema table — single source
    # of truth for "which subcategories are in scope."
    # Coerce to a real string-or-None so direct-call tests (which don't
    # go through FastAPI's Query() resolution) work the same as HTTP
    # requests do. Calling the handler with subcategory_group omitted
    # leaves the FieldInfo object in place; isinstance check handles
    # both shapes.
    group = subcategory_group if isinstance(subcategory_group, str) else None
    if group:
        supported_prefixes = BEAUTY_SUBCATEGORY_GROUPS.get(group, tuple())
    else:
        supported_prefixes = tuple(BEAUTY_SUBCATEGORY_SCHEMAS.keys())
    if not supported_prefixes:
        return {
            "status": "success",
            "data": {"queue": [], "totals": {
                "beauty_total": 0, "total_incomplete": 0,
                "page": page, "page_size": page_size, "has_more": False,
            }},
        }
    category_clause = " OR ".join(
        f"LOWER(cp.category_path) LIKE '{p}%'" for p in supported_prefixes
    )
    offset = (page - 1) * page_size

    # Pull all relevant data per product (the route then filters
    # per-subcategory). We always join beauty_* tables; for products in
    # subcategories that don't use a given field, the value is simply
    # ignored in the response.
    queue_sql = f"""
        WITH product_inci AS (
          SELECT
            bsi.product_key,
            BOOL_OR(bsi.raw_inci IS NOT NULL AND bsi.raw_inci != '') AS has_inci,
            BOOL_OR(bsi.source_system = 'merchant_payload') AS inci_payload_owned,
            (ARRAY_AGG(bsi.raw_inci ORDER BY bsi.updated_at DESC NULLS LAST))[1] AS sample_inci
          FROM beauty_sku_ingredients bsi
          GROUP BY bsi.product_key
        )
        SELECT
          cp.product_key,
          cp.platform,
          cp.source_product_id AS platform_product_id,
          cp.title,
          cp.image_url,
          cp.category_path,
          COALESCE(pi.has_inci, FALSE) AS has_inci,
          COALESCE(pi.inci_payload_owned, FALSE) AS inci_payload_owned,
          pi.sample_inci,
          bug.how_to_use_text,
          bpp.concerns_json,
          bpp.profile_payload
        FROM catalog_products cp
        LEFT JOIN product_inci pi ON pi.product_key = cp.product_key
        LEFT JOIN beauty_usage_guides bug ON bug.product_key = cp.product_key AND bug.sku_key IS NULL
        LEFT JOIN beauty_product_profiles bpp ON bpp.product_key = cp.product_key
        WHERE cp.merchant_id = :merchant_id
          AND ({category_clause})
        ORDER BY cp.last_seen_in_sync_at DESC NULLS LAST, cp.product_key ASC
    """

    rows = await database.fetch_all(queue_sql, {"merchant_id": merchant_id})

    # Per-row, figure out the subcategory schema, determine which fields
    # are missing for that subcategory, and only include products where
    # at least one applicable field is missing.
    eligible: List[Dict[str, Any]] = []
    per_subcategory_totals: Dict[str, Dict[str, int]] = {}
    per_subcategory_total: Dict[str, int] = {}

    for r in rows or []:
        row = dict(r)
        schema = beauty_subcategory_for_path(row.get("category_path"))
        if not schema:
            continue
        subcategory_kind = schema["subcategory_kind"]
        per_subcategory_total[subcategory_kind] = per_subcategory_total.get(subcategory_kind, 0) + 1

        profile_payload = row.get("profile_payload") or {}
        if isinstance(profile_payload, str):
            try:
                profile_payload = json.loads(profile_payload)
            except Exception:
                profile_payload = {}
        if not isinstance(profile_payload, dict):
            profile_payload = {}

        concerns = row.get("concerns_json")
        if isinstance(concerns, str):
            try:
                concerns = json.loads(concerns)
            except Exception:
                concerns = None

        # Build per-field state for THIS subcategory's fields only.
        fields_out: Dict[str, Any] = {}
        any_missing = False
        for field_spec in schema["fields"]:
            field_name = field_spec["name"]
            value: Any = None
            status = "missing"
            if field_name == "raw_inci":
                has_inci = bool(row.get("has_inci"))
                payload_owned = bool(row.get("inci_payload_owned"))
                value = row.get("sample_inci")
                status = (
                    "merchant-payload-locked" if payload_owned and has_inci
                    else "merchant-authored" if has_inci
                    else "missing"
                )
            elif field_name == "how_to_use_text":
                value = row.get("how_to_use_text")
                status = _beauty_field_status(value)
            elif field_name == "skin_concerns":
                value = concerns if isinstance(concerns, list) else None
                status = _beauty_field_status(value)
            else:
                # Subcategory-specific field stored in profile_payload JSONB.
                value = profile_payload.get(field_name)
                status = _beauty_field_status(value)

            fields_out[field_name] = {
                "status": status,
                "value": value,
                "confidence": None,
            }
            if status == "missing":
                any_missing = True
                bucket = per_subcategory_totals.setdefault(subcategory_kind, {})
                bucket[field_name] = bucket.get(field_name, 0) + 1

        if not any_missing:
            continue

        eligible.append({
            "platform": row["platform"],
            "platform_product_id": row["platform_product_id"],
            "title": row.get("title") or "Untitled product",
            "image_url": row.get("image_url"),
            "sku": None,
            "category_kind": "beauty",
            "subcategory_kind": subcategory_kind,
            "subcategory_label": schema["label"],
            "category_path": row.get("category_path"),
            "field_schemas": list(schema["fields"]),
            "fields": fields_out,
        })

    total_incomplete = len(eligible)
    beauty_total = sum(per_subcategory_total.values())
    paged = eligible[offset : offset + page_size]
    has_more = (page * page_size) < total_incomplete

    return {
        "status": "success",
        "data": {
            "queue": paged,
            "totals": {
                "beauty_total": beauty_total,
                "total_incomplete": total_incomplete,
                "page": page,
                "page_size": page_size,
                "has_more": has_more,
                # Per-subcategory breakdown — UI can show "Tools: 608 of
                # 608 need attention" alongside the global total.
                "per_subcategory": {
                    kind: {
                        "total": per_subcategory_total.get(kind, 0),
                        "missing_per_field": per_subcategory_totals.get(kind, {}),
                    }
                    for kind in per_subcategory_total
                },
            },
            "allowed_skin_concerns": list(ALLOWED_SKIN_CONCERNS),
            # Surface the full schema table so the UI can render the
            # form for each subcategory without hard-coding field metadata.
            "subcategory_schemas": [
                {
                    "subcategory_kind": s["subcategory_kind"],
                    "label": s["label"],
                    "fields": list(s["fields"]),
                }
                for s in BEAUTY_SUBCATEGORY_SCHEMAS.values()
            ],
        },
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
    standard_full = _build_standard_full(product_json)

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
