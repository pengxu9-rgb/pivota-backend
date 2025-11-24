"""
Admin Amazon Orders Routes

Handles Amazon SP-API order synchronization (admin only).
Creates import tasks that are processed by the catalog_import_worker.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from config.settings import settings
from utils.auth import require_admin
from db.platform_import_tasks import (
    create_import_task,
    get_import_task,
)
from db.connector_credentials import get_latest_connector_credential_for_merchant

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/amazon",
    tags=["Admin - Amazon Orders Sync"]
)


def ensure_amazon_sp_api_enabled():
    """Ensure Amazon SP-API feature is enabled."""
    if not getattr(settings, 'enable_amazon_sp_api', False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Amazon SP-API integration not enabled"
        )


class SyncOrdersRequest(BaseModel):
    merchant_id: str = Field(..., description="Merchant ID to sync orders for")
    created_after: Optional[str] = Field(None, description="ISO 8601 timestamp for order created_after filter (default: 7 days ago)")
    created_before: Optional[str] = Field(None, description="ISO 8601 timestamp for order created_before filter (default: now)")


class SyncOrdersResponse(BaseModel):
    task_id: int
    status: str
    message: str
    merchant_id: str
    created_after: str
    created_before: str


@router.post(
    "/sync-orders",
    response_model=SyncOrdersResponse,
    dependencies=[Depends(ensure_amazon_sp_api_enabled)]
)
async def sync_orders(
    payload: SyncOrdersRequest,
    current_admin: dict = Depends(require_admin)
) -> SyncOrdersResponse:
    """
    Create an Amazon orders sync task.
    
    The task will be processed by the catalog_import_worker.
    """
    # Verify merchant has Amazon credentials
    cred = await get_latest_connector_credential_for_merchant(
        payload.merchant_id,
        "amazon_sp_api"
    )
    
    if not cred:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No Amazon SP-API credentials found for merchant {payload.merchant_id}"
        )
    
    if not cred.get("is_valid", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Amazon SP-API credentials are invalid"
        )
    
    # Parse date filters
    if payload.created_after:
        try:
            created_after = datetime.fromisoformat(payload.created_after.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid created_after format (use ISO 8601)"
            )
    else:
        # Default: 7 days ago
        created_after = datetime.utcnow() - timedelta(days=7)
    
    if payload.created_before:
        try:
            created_before = datetime.fromisoformat(payload.created_before.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid created_before format (use ISO 8601)"
            )
    else:
        # Default: now
        created_before = datetime.utcnow()
    
    # Validate date range
    if created_after >= created_before:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="created_after must be before created_before"
        )
    
    # Create import task
    task_id = await create_import_task(
        merchant_id=payload.merchant_id,
        source_type="amazon_orders",
        connector="amazon_sp_api",
    )
    
    logger.info(
        f"Created Amazon orders sync task {task_id} for merchant {payload.merchant_id} "
        f"(created_after={created_after.isoformat()}, created_before={created_before.isoformat()})"
    )
    
    return SyncOrdersResponse(
        task_id=task_id,
        status="pending",
        message="Amazon orders sync task created successfully",
        merchant_id=payload.merchant_id,
        created_after=created_after.isoformat(),
        created_before=created_before.isoformat(),
    )


@router.get(
    "/sync-status/{task_id}",
    dependencies=[Depends(ensure_amazon_sp_api_enabled)]
)
async def get_sync_status(
    task_id: int,
    current_admin: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Get the status of an Amazon orders sync task.
    """
    task = await get_import_task(task_id)
    
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found"
        )
    
    # Verify it's an Amazon orders task
    if task.get("source_type") != "amazon_orders":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task is not an Amazon orders sync task"
        )
    
    return {
        "task_id": task["id"],
        "merchant_id": task["merchant_id"],
        "source_type": task["source_type"],
        "connector": task["connector"],
        "status": task["status"],
        "counts": task.get("counts"),
        "error": task.get("error"),
        "attempt": task.get("attempt", 0),
        "next_run_at": task.get("next_run_at"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }

