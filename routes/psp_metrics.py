"""PSP telemetry endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from services.merchant_psp_telemetry_service import (
    get_merchant_psp_telemetry,
    unavailable_payment_telemetry,
)
from utils.auth import MERCHANT_OR_ADMIN_ROLES, get_current_user

router = APIRouter()

@router.get("/merchant/psps/{psp_id}/metrics")
async def get_psp_metrics(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get measured telemetry for a specific PSP when available."""
    if current_user["role"] not in MERCHANT_OR_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP info
        psp_query = "SELECT psp_id, provider, merchant_id FROM merchant_psps WHERE psp_id = :psp_id"
        psp = await database.fetch_one(psp_query, {"psp_id": psp_id})
        
        if not psp:
            return unavailable_payment_telemetry()
        
        provider = psp["provider"]
        merchant_id = psp["merchant_id"]
        if current_user["role"] == "merchant" and str(current_user.get("merchant_id")) != str(merchant_id):
            raise HTTPException(status_code=404, detail="PSP not found")
        telemetry = await get_merchant_psp_telemetry(str(merchant_id), psp_id=psp_id)

        return {
            "provider": provider,
            **(telemetry.get(psp_id) or unavailable_payment_telemetry()),
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            **unavailable_payment_telemetry(),
            "error": str(e)
        }

@router.get("/merchant/psps/metrics/all")
async def get_all_psp_metrics(current_user: dict = Depends(get_current_user)):
    """Get measured telemetry for all PSPs of this merchant when available."""
    if current_user["role"] not in MERCHANT_OR_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required in current user context")
    
    try:
        # Get all PSPs
        psps_query = "SELECT psp_id, provider FROM merchant_psps WHERE merchant_id = :merchant_id"
        psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        telemetry = await get_merchant_psp_telemetry(str(merchant_id))
        
        metrics = {}
        for psp in psps:
            psp_id = psp["psp_id"]
            provider = psp["provider"]

            metrics[psp_id] = {
                "provider": provider,
                **(telemetry.get(psp_id) or unavailable_payment_telemetry()),
            }
        
        return {
            "status": "success",
            "data": metrics
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
