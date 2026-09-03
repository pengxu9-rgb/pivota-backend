"""Internal platform product sync endpoint for Agent catalog refreshes."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path, Query, status

from routes.universal_product_sync import UniversalSyncRequest, universal_product_sync

router = APIRouter(
    prefix="/agent/internal/platform/products",
    tags=["platform_products"],
)


async def require_platform_products_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("PROMOTIONS_ADMIN_KEY") or os.getenv("ADMIN_API_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.post("/sync/{merchant_id}", response_model=Dict[str, Any], status_code=status.HTTP_200_OK)
async def sync_platform_products_endpoint(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    platform: Optional[str] = Query(default=None, description="Optional platform hint, e.g. wix"),
    limit: int = Query(default=500, ge=1, le=5000),
    _: None = Depends(require_platform_products_admin),
) -> Dict[str, Any]:
    request = UniversalSyncRequest(
        merchant_id=merchant_id,
        force_refresh=True,
        limit=limit,
        platform=platform,
        # Nobody is waiting on this request: it is the gateway's scheduled
        # catalog auto-sync (X-ADMIN-KEY, server-to-server). Marks the
        # quality-backfill enqueue as dedupable.
        unattended=True,
    )
    result = await universal_product_sync(
        request=request,
        background_tasks=BackgroundTasks(),
        current_user={"role": "admin"},
    )
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    if payload.get("status") == "success":
        return {"ok": True, "summary": payload}
    if payload.get("status") == "warning":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "PLATFORM_PRODUCTS_WARNING", "message": payload.get("message"), "summary": payload},
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "PLATFORM_PRODUCTS_ERROR", "message": payload.get("message"), "summary": payload},
    )


@router.get("/sync/{merchant_id}", response_model=Dict[str, Any], status_code=status.HTTP_200_OK)
async def sync_platform_products_get(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    platform: Optional[str] = Query(default=None, description="Optional platform hint, e.g. wix"),
    limit: int = Query(default=500, ge=1, le=5000),
    _: None = Depends(require_platform_products_admin),
) -> Dict[str, Any]:
    return await sync_platform_products_endpoint(merchant_id=merchant_id, platform=platform, limit=limit)
