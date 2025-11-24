"""
Product sync router - Legacy compatibility layer

HTTP layer:
- Provides legacy /api/products/sync endpoints that redirect to the new
  universal sync routes.

Python API:
- Exposes SyncRequest and sync_products() so existing routes can call
  a stable function, which internally delegates to universal_product_sync.
"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from routes.universal_product_sync import (
    UniversalSyncRequest,
    UniversalSyncResponse,
    universal_product_sync,
)

router = APIRouter(prefix="/api/products", tags=["product-sync-legacy"])


@router.post("/sync")
async def legacy_sync_redirect():
    """Redirect to new universal sync endpoint"""
    # Legacy HTTP clients should move to /products/sync-universal/
    return RedirectResponse(url="/products/sync-universal/", status_code=307)


@router.get("/sync/status")
async def legacy_sync_status_redirect():
    """Redirect to new sync status endpoint"""
    # Status endpoint lives under /products/sync/status
    return RedirectResponse(url="/products/sync/status", status_code=307)


class SyncRequest(BaseModel):
    """
    Backwards-compatible request model used by:
    - routes.merchant_api_extensions
    - routes.employee_store_psp_fixes
    - routes.wix_sync
    """

    merchant_id: str
    force_refresh: bool = False
    limit: int = 50
    # Optional hint; universal sync still auto-detects platform
    platform: Optional[str] = None


# For callers that want a concrete type
SyncResult = UniversalSyncResponse


async def sync_products(
    request: SyncRequest,
    background_tasks: BackgroundTasks,
    current_user: dict,
) -> UniversalSyncResponse:
    """
    Backwards-compatible wrapper around universal_product_sync.

    Existing routes expect:
        sync_result.products_synced
        sync_result.platform
        sync_result.sync_time

    UniversalSyncResponse exposes the same fields, so we can return it directly.
    """
    universal_request = UniversalSyncRequest(
        merchant_id=request.merchant_id,
        force_refresh=request.force_refresh,
        limit=request.limit,
    )

    return await universal_product_sync(
        universal_request,
        background_tasks,
        current_user,
    )


# Note: Main product sync functionality is in:
# - universal_product_sync.py for multi-platform sync
# - product_routes.py for product CRUD operations
# - agent_products.py for agent-specific product management
