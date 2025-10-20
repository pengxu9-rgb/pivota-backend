"""Wix Product Sync Endpoint"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import get_current_user

router = APIRouter()

@router.post("/merchant/integrations/wix/sync")
async def sync_wix_products(current_user: dict = Depends(get_current_user)):
    """Sync products from Wix store"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # For now, return success message
    # In production, this would connect to Wix API and sync products
    return {
        "status": "success",
        "message": "Wix products sync queued. This feature is coming soon!"
    }

