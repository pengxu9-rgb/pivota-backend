from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query

from models.catalog import (
    CatalogSyncJobCreateRequest,
    CatalogSyncJobResponse,
    CatalogWebhookIngestResponse,
    IncentivesReconcileRequest,
    IncentivesReconcileResponse,
)
from services.catalog_sync_service import (
    create_catalog_sync_job,
    get_catalog_sync_job,
    rebuild_beauty_verticals_for_merchant,
    reconcile_catalog_incentives_for_merchant,
    record_catalog_sync_event,
    run_catalog_sync_job,
)
from services.shopify_products_sync import sync_shopify_products_for_merchant
from utils.auth import ADMIN_ROLES, get_current_user


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


def _normalize_merchant_id(value: Any) -> str:
    return str(value or "").strip()


def _has_catalog_scope(merchant_id: str, current_user: Dict[str, Any]) -> bool:
    """True when this caller may act on `merchant_id`.

    Admins act on any merchant; everyone else only on the merchant named by
    their own JWT claim. Same shape as the sibling catalog write in
    `routes/universal_product_sync.py`.
    """
    if str(current_user.get("role") or "") in ADMIN_ROLES:
        return True
    caller_merchant_id = _normalize_merchant_id(current_user.get("merchant_id"))
    # A token carrying no merchant_id claim must not match a blank requested
    # id — without the truthiness guard, "" == "" would wave it through.
    return bool(caller_merchant_id) and caller_merchant_id == _normalize_merchant_id(merchant_id)


def _require_catalog_scope(merchant_id: str, current_user: Dict[str, Any]) -> str:
    """Refuse catalog work aimed at a merchant the caller does not own.

    `get_current_user` accepts ANY valid JWT of ANY role and is used here only
    for a `requested_by` label, so without this check the `merchant_id` in the
    path/body/query is simply whatever the caller typed. Two of these routes
    reach `ingest_standard_products`, which writes `catalog_merchants`,
    `catalog_products`, `catalog_skus` and `catalog_offers` for that id —
    including ADR-009 `merch_obs_*` observed-seller ids that belong to no
    tenant, and so are reachable by admins only.

    Returns the NORMALIZED id, and every caller must use the return value
    downstream. Authorizing a stripped value while passing the raw one on lets
    a caller mint a `catalog_merchants` row keyed on "\xa0merch_owned\n" — the
    id that was authorized must be the id that gets written.
    """
    normalized = _normalize_merchant_id(merchant_id)
    if not _has_catalog_scope(normalized, current_user):
        raise HTTPException(
            status_code=403,
            detail="cannot run catalog jobs for another merchant",
        )
    return normalized


def _requested_by(current_user: Dict[str, Any], explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    for key in ("user_id", "employee_id", "email", "merchant_id", "id"):
        value = str(current_user.get(key) or "").strip()
        if value:
            return value
    return None


def _job_response(job: Dict[str, Any]) -> CatalogSyncJobResponse:
    return CatalogSyncJobResponse(
        job_id=str(job.get("job_id") or ""),
        merchant_id=str(job.get("merchant_id") or ""),
        connector=str(job.get("connector") or ""),
        mode=str(job.get("mode") or ""),
        status=str(job.get("status") or "pending"),
        scope=job.get("scope_json") or {},
        stats=job.get("stats_json") or {},
        error_message=job.get("error_message"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        created_at=job.get("created_at"),
        updated_at=job.get("updated_at"),
    )


async def _run_catalog_job_background(
    *,
    job_id: str,
    merchant_id: str,
    connector: str,
    limit: int,
    force_refresh: bool,
) -> None:
    try:
        if force_refresh and connector == "shopify":
            await sync_shopify_products_for_merchant(
                merchant_id=merchant_id,
                limit=limit,
                ingest_catalog=False,
            )
        await run_catalog_sync_job(job_id)
    except Exception as exc:  # pragma: no cover - background task
        logger.exception("Catalog job background execution failed job_id=%s merchant_id=%s err=%s", job_id, merchant_id, exc)


@router.post("/sync/jobs", response_model=CatalogSyncJobResponse)
async def create_sync_job(
    req: CatalogSyncJobCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> CatalogSyncJobResponse:
    merchant_id = _require_catalog_scope(req.merchant_id, current_user)
    scope = {
        "platform": req.platform or req.connector,
        "limit": req.limit,
        "include_expired": True,
        "source_system": "products_cache" if req.sync_from_cache else req.connector,
        "force_refresh": req.force_refresh,
    }
    if req.scope:
        scope.update(req.scope)

    job = await create_catalog_sync_job(
        merchant_id=merchant_id,
        connector=req.connector,
        mode=req.mode,
        scope=scope,
        requested_by=_requested_by(current_user, req.requested_by),
    )
    background_tasks.add_task(
        _run_catalog_job_background,
        job_id=str(job.get("job_id") or ""),
        merchant_id=merchant_id,
        connector=req.connector,
        limit=req.limit,
        force_refresh=req.force_refresh,
    )
    return _job_response(job)


@router.get("/sync/jobs/{job_id}", response_model=CatalogSyncJobResponse)
async def read_sync_job(
    job_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> CatalogSyncJobResponse:
    job = await get_catalog_sync_job(job_id)
    # 404 rather than 403 on a scope miss: a 403 would confirm that the job id
    # exists, turning this route into a job-id enumeration oracle.
    if not job or not _has_catalog_scope(str(job.get("merchant_id") or ""), current_user):
        raise HTTPException(status_code=404, detail="Catalog sync job not found")
    return _job_response(job)


@router.post("/connectors/shopify/webhooks", response_model=CatalogWebhookIngestResponse)
async def ingest_shopify_catalog_event(
    merchant_id: str = Query(..., description="Merchant ID"),
    event_type: str = Query(..., description="Event type"),
    topic: Optional[str] = Query(default=None),
    source_ref: Optional[str] = Query(default=None),
    payload: Dict[str, Any] = Body(default_factory=dict),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> CatalogWebhookIngestResponse:
    merchant_id = _require_catalog_scope(merchant_id, current_user)
    event = await record_catalog_sync_event(
        merchant_id=merchant_id,
        connector="shopify",
        event_type=event_type,
        topic=topic,
        payload_json=payload,
        source_ref=source_ref,
    )
    return CatalogWebhookIngestResponse(
        event_id=str(event.get("event_id") or ""),
        merchant_id=str(event.get("merchant_id") or ""),
        connector=str(event.get("connector") or "shopify"),
        event_type=str(event.get("event_type") or event_type),
        topic=event.get("topic"),
        status=str(event.get("status") or "pending"),
        created_at=event.get("created_at"),
    )


@router.post("/reconcile/merchants/{merchant_id}", response_model=CatalogSyncJobResponse)
async def reconcile_catalog_for_merchant(
    merchant_id: str,
    background_tasks: BackgroundTasks,
    connector: str = Query(default="shopify"),
    platform: Optional[str] = Query(default="shopify"),
    limit: int = Query(default=500, ge=1, le=5000),
    force_refresh: bool = Query(default=True),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> CatalogSyncJobResponse:
    merchant_id = _require_catalog_scope(merchant_id, current_user)
    job = await create_catalog_sync_job(
        merchant_id=merchant_id,
        connector=connector,
        mode="reconcile",
        scope={
            "platform": platform or connector,
            "limit": limit,
            "include_expired": True,
            "source_system": "products_cache",
            "force_refresh": force_refresh,
        },
        requested_by=_requested_by(current_user, None),
    )
    background_tasks.add_task(
        _run_catalog_job_background,
        job_id=str(job.get("job_id") or ""),
        merchant_id=merchant_id,
        connector=connector,
        limit=limit,
        force_refresh=force_refresh,
    )
    return _job_response(job)


@router.post("/verticals/beauty/rebuild/{merchant_id}", response_model=Dict[str, Any])
async def rebuild_beauty_verticals(
    merchant_id: str,
    platform: Optional[str] = Query(default="shopify"),
    limit: int = Query(default=1000, ge=1, le=10000),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _require_catalog_scope(merchant_id, current_user)
    return await rebuild_beauty_verticals_for_merchant(
        merchant_id=merchant_id,
        platform=platform,
        limit=limit,
    )


@router.post("/incentives/reconcile/{merchant_id}", response_model=IncentivesReconcileResponse)
async def reconcile_incentives(
    merchant_id: str,
    req: IncentivesReconcileRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> IncentivesReconcileResponse:
    merchant_id = _require_catalog_scope(merchant_id, current_user)
    result = await reconcile_catalog_incentives_for_merchant(
        merchant_id=merchant_id,
        payment_incentives=req.payment_incentives,
        source_system=req.source_system,
    )
    return IncentivesReconcileResponse(**result)
