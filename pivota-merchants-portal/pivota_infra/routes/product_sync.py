"""
Product sync router - Legacy compatibility layer
Actual implementation is in universal_product_sync.py
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/api/products", tags=["product-sync-legacy"])

@router.post("/sync")
async def legacy_sync_redirect():
    """Redirect to new universal sync endpoint"""
    return RedirectResponse(url="/products/sync/all", status_code=307)

@router.get("/sync/status")
async def legacy_sync_status_redirect():
    """Redirect to new sync status endpoint"""
    return RedirectResponse(url="/products/sync/status", status_code=307)

# Note: Main product sync functionality is in:
# - universal_product_sync.py for multi-platform sync
# - product_routes.py for product CRUD operations
# - agent_products.py for agent-specific product management
